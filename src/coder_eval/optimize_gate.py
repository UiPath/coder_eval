"""`/coder-eval:optimize-skill`'s promotion gate and preflights, over finalized run directories.

`/coder-eval:optimize-skill` decides whether a candidate skill description beats the incumbent.
That decision used to be `min(candidate F1) > max(incumbent F1)` across three invocations —
arithmetic the skill's agent did by hand, which throws away the pairing (both arms ran the SAME
rows) and has poor power at 8-12 rows per polarity. This module replaces it with a paired
cluster bootstrap over rows, computed by tested code.

**A library, not a CLI.** There is no Typer command and no ``__main__``; the skill drives these
functions from a short inline ``python`` snippet. So this module imports no CLI machinery — the
verdict renders as a plain markdown ``str``.

Same species as :mod:`coder_eval.reports_junit`: it reads a run directory's on-disk contract
(``<run>/<variant>/<suite_id>/<row_id>/NN/task.json``) and returns a model plus a string. It runs
after ``coder-eval run`` has finished and touches nothing in the evaluation flow.

**F1 is never recomputed here.** Every metric comes from
:func:`coder_eval.criteria._classification_aggregate.classification_metrics`, the criterion
layer's own routine (CE037), so the gate cannot disagree with the numbers the run reported.
"""

from __future__ import annotations

import logging
import math
import os
import statistics as _stats
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from coder_eval.criteria._classification_aggregate import classification_metrics
from coder_eval.leak_detection import graded_strings
from coder_eval.models import (
    TARGET_LABEL,
    ActivationGateVerdict,
    ArmRowScores,
    ClassificationCriterionResult,
    EvaluationResult,
    ExecutionGateVerdict,
    ExperimentResult,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    RegressionRow,
    RoundScores,
    TaskDefinition,
)
from coder_eval.reports_stats import (
    DEFAULT_ALPHA,
    bootstrap_p_floor,
    cluster_bootstrap_diff_ci,
    holm_rejections,
    mean,
    paired_comparison,
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

# What "at or near the bootstrap's resolution floor" means: a p within this multiple of the
# estimator's own 2/(m+1) is close enough that the DRAW COUNT, not the data, is plausibly deciding
# it — so the verdict says so and tells the reader to re-run with more draws before believing
# either answer. A suppression threshold on a warning, never a gate: nothing about a promotion
# turns on it. Named because a bare literal silently decides whether that warning fires at all.
NEAR_FLOOR_MULTIPLE = 5.0

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


def load_suite_rows(run_dir: Path, variant_id: str, suite_id: str) -> dict[str, list[EvaluationResult]]:
    """Every row's replicate results for one arm of one run, keyed by row id.

    Walks ``<run_dir>/<variant_id>/<suite_id>/<row_id>/NN/task.json``. A missing variant or suite
    directory returns an empty mapping rather than raising: a mistyped path is the documented
    silent-zero failure mode, and the right place to make it loud is the verdict, which reports
    ``rows_paired == 0`` and says so.

    A malformed ``task.json`` is logged and skipped (CE021) — the row is then simply absent on
    that side and falls into ``rows_excluded``.
    """
    rows: dict[str, list[EvaluationResult]] = {}
    suite_dir = run_dir / variant_id / suite_id
    if not suite_dir.is_dir():
        return rows

    for task_json in sorted(suite_dir.glob("*/[0-9][0-9]/task.json")):
        row_id = task_json.parent.parent.name
        try:
            result = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load %s for the optimize gate", task_json, exc_info=True)
            continue
        rows.setdefault(row_id, []).append(result)
    return rows


def _pool(per_invocation: list[dict[str, list[EvaluationResult]]]) -> dict[str, list[EvaluationResult]]:
    """Merge per-invocation row maps into one, appending replicates rather than overwriting."""
    merged: dict[str, list[EvaluationResult]] = {}
    for rows in per_invocation:
        for row_id, results in rows.items():
            merged.setdefault(row_id, []).extend(results)
    return merged


def load_arm_rows(run_dirs: Sequence[Path], variant_id: str, suite_id: str) -> dict[str, list[EvaluationResult]]:
    """One arm's rows pooled across its separate invocations.

    Stage B runs three separate ``coder-eval run`` commands, so a row appears once per run
    directory. Those are the row's replicates — the within-cluster data the bootstrap resamples —
    so they are appended, never overwritten.
    """
    return _pool([load_suite_rows(run_dir, variant_id, suite_id) for run_dir in run_dirs])


def _label_pairs(results: list[EvaluationResult], criterion_index: int) -> list[tuple[str, str]]:
    """``(expected_label, observed_label)`` for one criterion INSTANCE across some results.

    Selection is **positional**, mirroring ``reports._compute_suite_rollup``: the checker appends
    one result per criterion in declared order, so ``success_criteria_results[i]`` belongs to
    ``success_criteria[i]``. A description key would not work — both bundled templates interpolate
    ``${row.id}`` into every criterion description, so every row's description is a different
    string and a description-matched gate would pair zero rows on the very suites it ships for.

    A result list shorter than the index is skipped rather than indexed past its end.
    """
    pairs: list[tuple[str, str]] = []
    for result in results:
        if criterion_index >= len(result.success_criteria_results):
            continue
        criterion_result = result.success_criteria_results[criterion_index]
        if isinstance(criterion_result, ClassificationCriterionResult):
            pairs.append((criterion_result.expected_label, criterion_result.observed_label))
    return pairs


def _observed_result_types(rows: dict[str, list[EvaluationResult]], criterion_index: int) -> set[str]:
    """The result types actually sitting at ``criterion_index`` — for the wrong-index note."""
    found: set[str] = set()
    for results in rows.values():
        for result in results:
            if criterion_index < len(result.success_criteria_results):
                found.add(type(result.success_criteria_results[criterion_index]).__name__)
    return found


def _metric(pairs: list[tuple[str, str]], name: str) -> float:
    """A classification metric over ``pairs``, through the criterion layer's own routine.

    Empty pairs and an absent metric name both read 0.0, and both conventions are declared in
    ``_classification_aggregate`` rather than here — restating either at this call site would be
    the second declaration CE037 exists to prevent.
    """
    cm = classification_metrics(pairs)
    return cm.metric(name) if cm is not None else 0.0


def _f1_yes(pairs: list[tuple[str, str]]) -> float:
    return _metric(pairs, f"f1.{TARGET_LABEL}")


def resolve_model(rows: dict[str, list[EvaluationResult]]) -> str | None:
    """The model id these rows ran under, or ``None`` when it is not a single agreed value.

    ``None`` for unset, and ``None`` when the rows disagree — a mixed-model suite must never be
    cached under one model key, so an unresolvable model recomputes rather than borrowing another
    model's measurement. ``NoiseFloor.model`` (Phase 6) has no other source.
    """
    models = {result.model_used for results in rows.values() for result in results if result.model_used}
    return models.pop() if len(models) == 1 else None


def _row_costs(results: list[EvaluationResult]) -> list[float]:
    """Per-replicate total cost for one row, skipping replicates that recorded none."""
    return [
        result.total_token_usage.total_cost_usd
        for result in results
        if result.total_token_usage is not None and result.total_token_usage.total_cost_usd is not None
    ]


def _row_durations(results: list[EvaluationResult]) -> list[float]:
    return [result.duration_seconds for result in results]


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


def _median(values: list[float]) -> float | None:
    """The median, or ``None`` for an empty sample — distinct from a median that happens to be 0.0."""
    return _stats.median(values) if values else None


def _row_cost_levels(clusters: Sequence[list[float]]) -> list[float]:
    """One value per row: the mean over that row's measured replicates. Empty rows are absent.

    The single definition of "what a row measured", called by :func:`cost_latency_guardrails` — for
    **both** its cost and its latency clusters — and by :func:`cost_quality_points`. Two
    implementations of it is the CE037-class defect this repo already has a lint rule for, and one
    definition is what makes the agreement test between those two surfaces writable at all. Named
    for cost because that is the reduction the two surfaces have to agree about; latency rides the
    same arithmetic.

    Takes CLUSTERS rather than the raw row mapping, because that is the shape both callers actually
    share: the guardrail reduces clusters it has already paired and balanced between two arms, and
    the N-arm view reduces one arm's clusters directly. A signature taking the row mapping could
    only serve the second, which would leave the duplication in place.
    """
    return [mean(c) for c in clusters if c]


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
        comparable = [(inc[: min(len(inc), len(cand))], cand[: min(len(inc), len(cand))]) for inc, cand in paired]
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


MEASUREMENTS_FILENAME = "measurements.json"


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    A process crash mid-write cannot leave a truncated file behind, and a failed replace cleans up
    after itself rather than leaving a temp sibling for the next reader to wonder about.

    Two limits, stated rather than defended against, because this is a local single-agent artifact:
    the read-modify-write around this call is **not** locked, so two concurrent writers lose one
    set of changes — tolerable for the noise-floor cache, which recomputes, and a real (accepted)
    loss for the regression corpus, which does not. And ``os.replace`` follows a symlink at
    ``path``, replacing the link rather than its target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_measurements(path: Path) -> OptimizeMeasurements:
    """Read the sidecar. A missing file is empty; a malformed one RAISES.

    A corrupt cache is not silently rebuilt. A silently-rebuilt cache is indistinguishable from a
    correct one, and the regression corpus it carries is not reconstructible from anything else —
    so the failure has to be loud, with the path in the message.

    The file lives at ``.optimize-skill/<skill>/measurements.json``, so its ``skill`` field must
    match the parent directory name. A mismatch means the file was copied by hand from another
    skill, and merging it would quietly attribute one skill's measurements to another.
    """
    skill = path.parent.name
    if not path.exists():
        return OptimizeMeasurements(skill=skill)
    try:
        measurements = OptimizeMeasurements.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"malformed optimize measurements at {path}: {exc}") from exc
    if measurements.skill != skill:
        raise ValueError(
            f"optimize measurements at {path} belong to skill {measurements.skill!r}, "
            + f"but the path says {skill!r} — the file was copied rather than written here"
        )
    return measurements


# Every NoiseFloor field except the measurement itself. Derived from the model rather than
# listed twice, so a new key field cannot be added to NoiseFloor and forgotten here — which is
# the mistake that turns a cache into a source of foreign numbers.
_FLOOR_MEASUREMENT_FIELDS = frozenset({"mde", "computed_at"})


def _floor_key(floor: NoiseFloor) -> tuple[object, ...]:
    return tuple(getattr(floor, name) for name in NoiseFloor.model_fields if name not in _FLOOR_MEASUREMENT_FIELDS)


def record_noise_floor(path: Path, floor: NoiseFloor) -> OptimizeMeasurements:
    """Cache a measured floor, replacing any entry measured under identical conditions.

    Replacement rather than append, because this file is a cache plus a corpus — not a record of
    what happened. That is exactly the distinction that keeps the narrative ledger free-form and
    append-only next door.
    """
    measurements = load_measurements(path)
    if floor.model == UNRESOLVED_MODEL:
        logger.info("Not caching a noise floor measured under an unresolved model — it could never match a lookup")
        return measurements
    key = _floor_key(floor)
    kept = [f for f in measurements.noise_floors if _floor_key(f) != key]
    updated = measurements.model_copy(update={"noise_floors": [*kept, floor]})
    _atomic_write(path, updated.model_dump_json(indent=2))
    return updated


def lookup_noise_floor(measurements: OptimizeMeasurements, probe: NoiseFloor) -> NoiseFloor | None:
    """The cached floor measured under conditions identical to ``probe``, else ``None``.

    ``probe`` is a fully-populated :class:`NoiseFloor` whose ``mde`` and ``computed_at`` are
    ignored — passing the record you are about to write is what makes it impossible to look up on
    a subset of the key and be handed a number from a different measurement.

    Scans newest-first, so a hand-edited file carrying two entries for one key resolves to the
    later of them rather than the stale one.
    """
    key = _floor_key(probe)
    for floor in reversed(measurements.noise_floors):
        if _floor_key(floor) == key:
            return floor
    return None


def append_regression_rows(path: Path, rows: list[RegressionRow]) -> OptimizeMeasurements:
    """Append to the corpus, de-duplicated on ``row_id``.

    Append-only: re-promoting a row already in the corpus is a no-op, never a duplicate entry and
    never a rewrite of why it was added the first time.
    """
    measurements = load_measurements(path)
    seen = {row.row_id for row in measurements.regression_corpus}
    fresh: list[RegressionRow] = []
    for row in rows:
        if row.row_id not in seen:
            seen.add(row.row_id)
            fresh.append(row)
    if not fresh:
        return measurements
    updated = measurements.model_copy(update={"regression_corpus": [*measurements.regression_corpus, *fresh]})
    _atomic_write(path, updated.model_dump_json(indent=2))
    return updated


def regression_check(
    corpus: list[RegressionRow], arm: ArmRowScores, *, threshold: float = 1.0
) -> list[tuple[RegressionRow, float | None]]:
    """Corpus rows this arm did not fully score — the promotions it would quietly undo.

    The corpus is written on every promotion (:func:`append_regression_rows`) and, until this
    function existed, read by nothing but that writer's own de-duplication. This is the read: a
    candidate that re-loses a row an earlier promotion was built on is a regression however good
    its aggregate looks, and an aggregate cannot show it.

    One entry per corpus row that did not clear ``threshold``, in corpus order:

    - ``(row, score)`` when the arm scored it below the bar — a measured loss.
    - ``(row, None)`` when the arm has no score for it at all. **A hole is reported, never
      skipped**, the same rule :func:`_dominates` applies to the row vector: not measuring a row is
      not passing it. The two causes are indistinguishable from the corpus alone — the row errored
      in this run, or it belongs to the skill's OTHER suite, since the corpus is per skill and a
      skill may have both an activation and an outcome suite. Check which before reporting it.

    Rows at or above ``threshold`` are absent, so an empty result is the clean answer.

    ``threshold`` defaults to 1.0, which treats any partial score as a loss. That is right for the
    binary activation criterion the corpus is usually written from; the parameter exists for a
    fractional execution suite. Note that ``arm.row_scores`` values are means over replicates, so a
    row that passed 2 of 3 replicates reads 0.667 and is reported at the default — correctly, since
    a row that became flaky is a row the promotion no longer holds on.
    """
    findings: list[tuple[RegressionRow, float | None]] = []
    for row in corpus:
        score = arm.row_scores.get(row.row_id)
        if score is None or score < threshold:
            findings.append((row, score))
    return findings


def record_round_scores(path: Path, scores: RoundScores) -> OptimizeMeasurements:
    """Append one round's row vectors and Pareto front, replacing an entry for the same round.

    Replacement per round, like the noise-floor cache and unlike the regression corpus: re-running
    a round supersedes its measurement rather than adding a second, contradictory one. The vectors
    are stored whole and never truncated — being able to look back at which rows a DISCARDED
    candidate won is the entire reason they are kept rather than an average.
    """
    measurements = load_measurements(path)
    kept = [r for r in measurements.round_scores if r.round != scores.round]
    updated = measurements.model_copy(update={"round_scores": [*kept, scores]})
    _atomic_write(path, updated.model_dump_json(indent=2))
    return updated


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

    To RECORD what this measured, call :func:`measure_noise_floor` instead — it returns the whole
    keyed record, including the row count, which this function does not expose.
    """
    measured = measure_noise_floor(
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


# Placeholder for "the caller resolved no single model" — a mixed-model or unlabelled suite. It can
# never collide with a real model id. A floor carrying it is neither read from nor written to the
# cache: borrowing another model's floor is worse than recomputing, and storing one under this key
# would accumulate entries that can never match their own lookup. PUBLIC because the skill's
# snippet needs to spell it; a literal in the prose would silently become a REAL key if this value
# ever changed.
UNRESOLVED_MODEL = "(unresolved)"


def _no_floor(reason: str) -> None:
    """Log why a null comparison could not be made, and return None.

    Both floor functions return ``None`` for several distinct reasons, and the caller is an agent
    about to decide whether to spend money. A silent ``None`` is indistinguishable from a floor of
    zero to anyone not reading the source — and it was silent: verified against the shipped code,
    ``noise_floor_mde`` with a mistyped run directory returned a bare ``None`` and printed nothing,
    on the one function whose job is to stop a user spending.

    An unconfigured ``logging.warning`` reaches stderr through Python's last-resort handler, so the
    agent driving the skill's inline snippet sees this without any logging setup.
    """
    logger.warning("No noise floor could be computed: %s", reason)
    return None


def _floor_from_clusters[T](
    clusters_a: list[list[T]],
    clusters_b: list[list[T]],
    statistic: Callable[[list[T]], float],
    probe: NoiseFloor,
    measurements: OptimizeMeasurements | None,
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
        return _no_floor(f"the bootstrap declined on {len(clusters_a)} cluster(s) for {probe.suite_id!r} — it needs 2")
    _diff, ci_low, ci_high, _p = bootstrap
    return probe.model_copy(update={"mde": (ci_high - ci_low) / 2.0})


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
    """
    if len(run_dirs) < 2:
        return _no_floor(f"the null split needs at least 2 invocations of {variant_id!r}, got {len(run_dirs)}")

    per_dir = [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
    # The same wrong-path guard `measure_execution_noise_floor` carries, and for the same reason: a
    # mistyped variant, suite or run directory is the documented SILENT-ZERO failure mode, and
    # without it the reader is told "only 0 row(s) scored a classification result at criterion N"
    # and goes off to check the criterion index instead of the path.
    #
    # `not any(per_dir)` rather than the twin's `not rows`: that one pools once, this one keeps the
    # per-invocation maps because it splits them into halves.
    if not any(per_dir):
        searched = ", ".join(str(d) for d in run_dirs)
        return _no_floor(
            f"nothing matched <run>/{variant_id}/{suite_id}/*/NN/task.json under {searched} — that "
            + "is a wrong variant id, a wrong suite id or a wrong run directory, not a measurement"
        )

    midpoint = (len(per_dir) + 1) // 2
    first, second = _pool(per_dir[:midpoint]), _pool(per_dir[midpoint:])

    shared = sorted(set(first) & set(second))
    clusters_a = [_label_pairs(first[rid], criterion_index) for rid in shared]
    clusters_b = [_label_pairs(second[rid], criterion_index) for rid in shared]
    scored = [(a, b) for a, b in zip(clusters_a, clusters_b, strict=True) if a and b]
    if len(scored) < 2:
        return _no_floor(
            f"only {len(scored)} row(s) of {suite_id!r} scored a classification result at criterion "
            + f"{criterion_index} in BOTH halves of the invocation split — an interval needs 2"
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
        mde=0.0,
        computed_at=datetime.now(UTC),
    )
    return _floor_from_clusters([a for a, _b in scored], [b for _a, b in scored], _f1_yes, probe, measurements)


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
    rows = _pool([load_suite_rows(d, variant_id, suite_id) for d in run_dirs])
    # A mistyped variant, suite or run directory is the documented SILENT-ZERO failure mode, and
    # "no row carries 2+ replicates" would send the reader off to check --repeats instead of the
    # path. Distinguished here for the same reason `activation_gate` distinguishes it.
    if not rows:
        searched = ", ".join(str(d) for d in run_dirs) or "no run dirs were given"
        return _no_floor(
            f"nothing matched <run>/{variant_id}/{suite_id}/*/NN/task.json under {searched} — that "
            + "is a wrong variant id, a wrong suite id or a wrong run directory, not a measurement"
        )

    # `criterion_index=None` is already the "read the row's weighted_score" mode, so this reuses
    # the existing extractor rather than adding a second definition of what a row scored.
    replicated: list[list[float]] = []
    for _row_id, results in sorted(rows.items()):
        values = [v for r in results if (v := _row_score(r, None)) is not None]
        if len(values) >= 2:
            replicated.append(values)
    if len(replicated) < 2:
        return _no_floor(
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
        mde=0.0,
        computed_at=datetime.now(UTC),
    )
    return _floor_from_clusters([a for a, _b in halves], [b for _a, b in halves], mean, probe, measurements)


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
    """
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
        incumbent = _label_pairs(incumbent_rows.get(row_id, []), index)
        candidate = _label_pairs(candidate_rows.get(row_id, []), index)
        keep = min(len(incumbent), len(candidate))
        per_row.append((incumbent[:keep], candidate[:keep]))
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
        raw_incumbent = [p for rid in paired_row_ids for p in _label_pairs(incumbent_rows.get(rid, []), index)]
        raw_candidate = [p for rid in paired_row_ids for p in _label_pairs(candidate_rows.get(rid, []), index)]
        per_row = _balanced_sibling_pairs(incumbent_rows, candidate_rows, paired_row_ids, index)
        incumbent_pairs = [p for inc, _c in per_row for p in inc]
        candidate_pairs = [p for _i, cand in per_row for p in cand]
        incumbent_recall = _metric(incumbent_pairs, metric_name)
        candidate_recall = _metric(candidate_pairs, metric_name)

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
    incumbent_by_dir = [load_suite_rows(d, incumbent_variant, suite_id) for d in incumbent_run_dirs]
    candidate_by_dir = [load_suite_rows(d, candidate_variant, suite_id) for d in candidate_run_dirs]
    incumbent_rows = _pool(incumbent_by_dir)
    candidate_rows = _pool(candidate_by_dir)

    paired_row_ids = sorted(set(incumbent_rows) & set(candidate_rows))
    unpaired = sorted((set(incumbent_rows) | set(candidate_rows)) - set(paired_row_ids))

    notes: list[str] = []
    # A mistyped variant or suite is the documented SILENT-ZERO failure mode, and its symptom —
    # zero rows — is indistinguishable from a genuinely tiny suite unless the verdict says which.
    for arm, variant_id, rows, run_dirs in (
        ("incumbent", incumbent_variant, incumbent_rows, incumbent_run_dirs),
        ("candidate", candidate_variant, candidate_rows, candidate_run_dirs),
    ):
        if not rows:
            searched = ", ".join(str(d) for d in run_dirs) or "no run dirs were given"
            notes.append(
                f"the {arm} arm loaded ZERO rows: nothing matched "
                + f"<run>/{variant_id}/{suite_id}/*/NN/task.json under {searched}. "
                + "That is a wrong variant id, a wrong suite id or a wrong run directory — not a result. "
                + "Fix the path before reading anything below."
            )

    if unpaired:
        notes.append(
            f"{len(unpaired)} row(s) present in only one arm and excluded from the pairing: {', '.join(unpaired)}. "
            + "An asymmetric sample produces confident nonsense — find out why before reading the interval."
        )

    per_row = {
        rid: (_label_pairs(incumbent_rows[rid], criterion_index), _label_pairs(candidate_rows[rid], criterion_index))
        for rid in paired_row_ids
    }

    # A row that errored or timed out is written with an EMPTY success_criteria_results — the row
    # directory exists, so it pairs, but it scores on one arm only. Left in, the two arms' F1s are
    # computed over different row sets, and the bias runs toward the candidate: an edit that makes
    # the agent crash on exactly the rows it was failing would score a perfect F1 on what is left.
    # So a row scored on only one arm is dropped from BOTH vectors and counted, mirroring the suite
    # rollup's exclude-then-report convention rather than being silently absorbed.
    hollow = sorted(rid for rid, (inc, cand) in per_row.items() if bool(inc) != bool(cand))
    scored_row_ids = [rid for rid in paired_row_ids if all(per_row[rid])]
    unscored_count = len(paired_row_ids) - len(scored_row_ids)

    if hollow:
        notes.append(
            f"{len(hollow)} row(s) scored on only one arm for criterion {criterion_index} and were "
            + f"excluded from both: {', '.join(hollow)}. A row that errored or timed out produces no "
            + "criterion result, and comparing arms over different row sets favours whichever arm "
            + "failed to produce one."
        )

    # BALANCE the replicate counts per row before pooling. A row's weight in an arm's f1.yes is
    # its number of observations, so an arm that contributed 3 replicates for a row while the other
    # contributed 2 has silently reweighted the comparison — and the trigger is mundane: Stage B is
    # three separate invocations, and one interrupted run leaves a partial row set. Measured, two
    # arms with BYTE-IDENTICAL labels on every row produced f1 0.818 vs 0.750 with an interval
    # excluding zero and rows_excluded == 0. Truncating each row to min(n_incumbent, n_candidate)
    # makes every row weigh the same on both sides; the dropped observations are counted and noted.
    balanced: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
    dropped = 0
    for rid in scored_row_ids:
        inc, cand = per_row[rid]
        keep = min(len(inc), len(cand))
        dropped += len(inc) + len(cand) - 2 * keep
        balanced[rid] = (inc[:keep], cand[:keep])
    unbalanced_rows = [rid for rid in scored_row_ids if len(per_row[rid][0]) != len(per_row[rid][1])]
    if unbalanced_rows:
        notes.append(
            f"{len(unbalanced_rows)} row(s) had different replicate counts on the two arms and were "
            + f"trimmed to the smaller count, dropping {dropped} observation(s): "
            + f"{', '.join(unbalanced_rows)}. A row's weight in an arm's F1 is its observation count, "
            + "so an unbalanced row shifts the comparison on its own — usually an interrupted "
            + "invocation. Re-run it rather than reading the interval below as an effect."
        )

    incumbent_clusters = [balanced[rid][0] for rid in scored_row_ids]
    candidate_clusters = [balanced[rid][1] for rid in scored_row_ids]

    incumbent_pairs = [p for cluster in incumbent_clusters for p in cluster]
    candidate_pairs = [p for cluster in candidate_clusters for p in cluster]

    if paired_row_ids and not (incumbent_pairs or candidate_pairs):
        found = _observed_result_types(incumbent_rows, criterion_index) | _observed_result_types(
            candidate_rows, criterion_index
        )
        notes.append(
            f"criterion_index={criterion_index} selected NO classification results on either arm "
            + f"(result types found at that position: {sorted(found) or 'none — the index is past the end'}). "
            + "This is a wiring mistake, not a measurement: the index is the criterion's POSITION in the "
            + "suite's success_criteria list. It is NOT the same as the skill never firing, which yields "
            + "pairs with observed='no'."
        )

    # A row is DISCORDANT when the arms' pooled pair multisets differ — `sorted`, not `==`, so a
    # row whose two arms carry the same pairs in a different replicate order counts as concordant.
    # Only discordant rows can move a resample's difference off exactly 0.0, which is what makes
    # `_discreteness_floor` a valid bound on the smallest p this suite can be expected to express.
    n_discordant = sum(1 for rid in scored_row_ids if sorted(balanced[rid][0]) != sorted(balanced[rid][1]))

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
    mde = noise_floor_mde(
        run_dirs=incumbent_run_dirs,
        variant_id=incumbent_variant,
        suite_id=suite_id,
        criterion_index=criterion_index,
        confidence=confidence,
        seed=seed,
        n_resamples=n_resamples,
    )
    p_floor = _discreteness_floor(len(scored_row_ids), n_discordant, n_resamples)

    bootstrap = cluster_bootstrap_diff_ci(
        candidate_clusters,
        incumbent_clusters,
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
            rows_excluded=len(unpaired) + unscored_count,
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
        notes.append(
            "the minimum detectable effect could not be computed (a null comparison needs at least two "
            + "invocations of the incumbent), so nothing here says how small a difference this suite can resolve."
        )

    # Retained as a DIAGNOSTIC, never as the gate: the per-invocation ranges are what the old
    # rule compared, and reporting them keeps a reader's intuition calibrated against the CI.
    incumbent_per_invocation = [
        _f1_yes([p for rid in scored_row_ids if rid in rows for p in _label_pairs(rows[rid], criterion_index)])
        for rows in incumbent_by_dir
    ]
    candidate_per_invocation = [
        _f1_yes([p for rid in scored_row_ids if rid in rows for p in _label_pairs(rows[rid], criterion_index)])
        for rows in candidate_by_dir
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
        rows_excluded=len(unpaired) + unscored_count,
        incumbent_f1=_f1_yes(incumbent_pairs),
        candidate_f1=_f1_yes(candidate_pairs),
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


def holm_promote(verdicts: list[ActivationGateVerdict], alpha: float = DEFAULT_ALPHA) -> list[ActivationGateVerdict]:
    """Decide the whole survivor family at once, and record the decision on each verdict.

    With ``S`` survivors gated against the same incumbent on the same rows, the family-wise error
    rate inflates. Holm's step-down corrects it — and it is a property of the FAMILY, so this
    calls :func:`coder_eval.reports_stats.holm_rejections` **once** across the whole p-value
    vector. Calling it per candidate would degenerate to an uncorrected ``p <= alpha``; dividing
    alpha by the survivor count at each gate would be plain Bonferroni, which is not Holm.

    A verdict whose ``p_value`` is ``None`` (too few paired rows) is not part of the family: it is
    excluded from the vector so it cannot tighten the correction for the others, and comes back
    ``promoted=False``.

    Promotion also requires the difference to favour the candidate and every sibling check to
    hold. The cost/latency guardrails stay advisory here — the skill's prose gates on them.

    **A suite whose discreteness floor exceeds its Holm threshold is REFUSED, not rejected.** The
    corrected threshold can sit below what the suite's own row count can express, and then no
    candidate can promote however good it is — reporting that as an ordinary negative result is a
    claim about the candidates that the data cannot support. Such a verdict comes back with
    ``gate_refusal`` set and ``promoted=False``, and renders as its own headline.
    """
    family = [(i, v.p_value) for i, v in enumerate(verdicts) if v.p_value is not None]
    rejections = holm_rejections([p for _i, p in family], alpha)
    rejected_at = {i for (i, _p), reject in zip(family, rejections, strict=True) if reject}

    decided: list[ActivationGateVerdict] = []
    for i, verdict in enumerate(verdicts):
        notes = list(verdict.notes)
        if verdict.p_value is None:
            notes.append("not promoted: the sample could not support a p-value, so this arm is outside the family.")
            decided.append(verdict.model_copy(update={"promoted": False, "holm_alpha": alpha, "notes": notes}))
            continue

        threshold = _holm_threshold([p for _i, p in family], verdict.p_value, alpha)
        # A floor above the bar the p is decided against means the gate structurally cannot
        # separate here — a statement about the SUITE, not about this candidate.
        refusal: str | None = None
        if verdict.p_floor is not None and verdict.p_floor > threshold:
            # A floor of exactly 1.0 is the ZERO-DISCORDANT case and nothing else: `2*(1-R/M)**M`
            # reaches 1.0 only at R = 0 (for R >= 1 it peaks at 0.5, at M = 2). It is a different
            # finding with a different remedy — the arms behaved identically, so the discordance
            # RATE is zero and `(1-R/M)**M` does not shrink with M however many rows are added.
            # Telling that operator to buy more rows is the misdiagnosis this branch exists to stop.
            if verdict.p_floor >= 1.0:
                refusal = (
                    f"the two arms produced identical labels on every one of the {verdict.rows_paired} "
                    "scored rows, so there is nothing for any test to separate — at any family size, "
                    "at any alpha, and at any number of rows. That is a result about this candidate "
                    "rather than about the suite: adding rows cannot change it. Check the candidate "
                    "actually differs from the incumbent, and that both arms were wired to the "
                    "snapshots you think they were."
                )
            else:
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
                # Rank-scoped, deliberately. `p_floor` is a property of the SUITE and identical
                # across the family, but `threshold` depends on rank — so where the floor sits
                # between alpha/S and alpha/(S-1) a worse-ranked sibling is NOT refused and may
                # still promote. Claiming "no candidate can promote here" would contradict it.
                refusal = (
                    f"this suite cannot express a p below {verdict.p_floor:.4f} at {verdict.rows_paired} "
                    f"paired rows, and the Holm threshold for this rank in a family of {len(family)} is "
                    f"{threshold:.4f}. This candidate could not have promoted however good it is — the "
                    f"bar sits below what the suite can measure — so this is NOT a negative result "
                    f"about it. {remedy}"
                )

        siblings_hold = all(check.passed for check in verdict.sibling_checks)
        favours_candidate = verdict.mean_diff is not None and verdict.mean_diff > 0.0
        # The interval must exclude zero as well as the corrected test rejecting. Holm is the
        # stricter of the two almost always, so this changes nothing on a typical family — but it
        # keeps "promote when the interval excludes zero" literally true, which is how the method
        # file states the rule and how anyone reading the rendered block will check it.
        excludes_zero = verdict.ci_low is not None and verdict.ci_low > 0.0
        # `refusal is None` is LOAD-BEARING, not belt-and-braces. `p_floor` bounds the p's
        # EXPECTATION, so a realized p dips below it on roughly half of all seeds (measured: 16 of
        # 30 on the 6-row fixture at 20,000 draws). Without this conjunction an unpromotable suite
        # promotes on a coin-flip AND carries a refusal — two contradictory claims in one block,
        # and the defect this whole field exists to fix, reborn.
        promoted = i in rejected_at and favours_candidate and siblings_hold and excludes_zero and refusal is None
        if i in rejected_at and favours_candidate and siblings_hold and not excludes_zero:
            notes.append(
                "not promoted: the Holm-corrected test rejects but the confidence interval still "
                + "contains zero, so the effect is not separated at the reported interval width."
            )
        if i in rejected_at and not siblings_hold:
            notes.append(
                "not promoted: the interval separates but a sibling's recall.yes dropped — this candidate "
                + "moved the failure rather than fixing it."
            )
        if i in rejected_at and not favours_candidate:
            notes.append("not promoted: the interval separates in the incumbent's favour.")
        if i not in rejected_at and refusal is None:
            notes.append(
                f"not promoted: p = {verdict.p_value:.4f} did not clear the Holm threshold for its rank "
                + f"in a family of {len(family)} (alpha={alpha}). This is the ordinary negative result — "
                + "the interval and the effect size above are what to report."
            )
        # A p at the resample floor is a resolution statement, not a measurement: the corrected
        # threshold can sit BELOW what the bootstrap can express, and then no candidate can ever
        # promote however good it is. Measured: 4 perfect candidates at 8 rows flip from all-rejected
        # to all-promoted between 2,000 and 20,000 resamples on identical data.
        estimator_floor = bootstrap_p_floor(verdict.n_resamples)
        if verdict.p_value <= NEAR_FLOOR_MULTIPLE * estimator_floor:
            notes.append(
                f"p = {verdict.p_value:.4f} is at or near this bootstrap's resolution floor "
                + f"({estimator_floor:.4f} at {verdict.n_resamples} draws), and the Holm threshold for "
                + f"this rank is {threshold:.4f}. Where the threshold approaches the floor the decision is "
                + "being made by the resample count rather than by the data — re-run the gate with a larger "
                + "n_resamples before believing either answer. A small suite has its own coarser floor: with "
                + "few positive rows the smallest achievable p is bounded well above the estimator's."
            )
        notes.append(f"Holm applied across a family of {len(family)} at alpha={alpha}.")
        # The refusal lives on `gate_refusal` and NOT in `notes`: notes is the "everything the
        # reader needs to distrust the numbers" channel, a refusal is a headline, and duplicating
        # it would print the same sentence twice in one block.
        decided.append(
            verdict.model_copy(
                update={"promoted": promoted, "holm_alpha": alpha, "gate_refusal": refusal, "notes": notes}
            )
        )
    return decided


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
        incumbent_pairs = [p for rid in row_ids for p in _label_pairs(incumbent_rows.get(rid, []), index)]
        candidate_pairs = [p for rid in row_ids for p in _label_pairs(candidate_rows.get(rid, []), index)]
        if not incumbent_pairs and not candidate_pairs:
            found = _observed_result_types(incumbent_rows, engagement_criterion_index) | _observed_result_types(
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
            incumbent_recall = _metric(incumbent_pairs, metric_name)
            candidate_recall = _metric(candidate_pairs, metric_name)
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

    Every failure mode — a missing, unreadable or malformed ``experiment.json``, an experiment with
    other than two variants, **either** variant id not matching it — returns a verdict whose
    statistics are ``None`` with a note naming what failed. Never an exception, and never a silent
    zero. The faults that do NOT show up in the statistic set ``gate_refusal`` instead, which forces
    ``promoted=False`` and takes the headline: an arm whose rows are not on disk (the statistic
    comes from ``experiment.json``, so it computes perfectly well over nothing), and paired
    differences with zero variance (it computes, and every promotion conjunct then holds at once).

    Leaves ``promoted=None``: one gate knows nothing about its family.
    """
    notes: list[str] = []
    if incumbent_variant == candidate_variant:
        # Otherwise the sign resolves off the candidate, matches vid_a, and the block reports
        # `vid_a - vid_b` labelled `candidate - incumbent` with both labels reading the same name
        # — a confident, significant, sign-flipped verdict comparing an arm to the other arm while
        # claiming to compare it to itself. Measured before this guard existed.
        notes.append(
            f"incumbent_variant and candidate_variant are both {incumbent_variant!r}, so there is no "
            + "comparison to make and no sign to resolve. Nothing below is a result."
        )
    incumbent_rows = load_arm_rows([run_dir], incumbent_variant, suite_id)
    candidate_rows = load_arm_rows([run_dir], candidate_variant, suite_id)
    row_ids = sorted(set(incumbent_rows) & set(candidate_rows))

    # The statistic comes from `experiment.json` while every check comes from the on-disk row tree,
    # so the two can disagree — and a valid experiment file beside a mistyped variant, suite or run
    # directory renders as PROMOTED with every check a green `— -> —`. `activation_gate` carries
    # this note for the same reason; without it the silent-zero failure mode is loud on one track
    # and silent on the other.
    #
    # Declared here, above the first condition that can set it, and read by `_verdict`'s shared
    # base so EVERY return path reports it. Two causes share one field — see
    # ExecutionGateVerdict.gate_refusal — and the first one set wins.
    gate_refusal: str | None = None
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
        gate_refusal = (
            f"{named} loaded ZERO rows: nothing matched "
            f"<run>/<variant>/{suite_id}/*/NN/task.json under {run_dir}. That is a wrong variant "
            "id, a wrong suite id or a wrong run directory. Every guardrail and integrity check "
            "below is computed over the rows that DID load, so they all pass — over nothing when "
            "both arms are empty, and as a large candidate improvement when only one is — and the "
            "paired statistic comes from experiment.json, which can still be perfectly valid. "
            "Fix the path before reading anything below."
        )

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
            gate_refusal=gate_refusal,
            mde=mde,
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
        notes.append(
            f"no experiment file at {experiment_json} — the paired statistic could not be computed. "
            + "A plain `coder-eval run` without `-e <experiment>` writes none, and this track's gate "
            + "is a two-variant experiment. Nothing below is a result about the candidate."
        )
        return _verdict()
    try:
        result = ExperimentResult.model_validate_json(experiment_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # OSError as well as ValueError: the docstring promises "Never an exception", and an
        # unreadable file (permissions, or one that vanished between the is_file() and the read) is
        # exactly as much a wiring fault as a malformed one — with the same right answer.
        logger.warning("Failed to load %s for the execution gate", experiment_json, exc_info=True)
        notes.append(
            f"the experiment file at {experiment_json} could not be read or parsed, so no statistic was computed."
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
    comparison = paired_comparison(result.model_copy(update={"per_replicate_scores": scoped_scores}), confidence)
    if comparison is None:
        # `variant_ids` is deliberately NOT narrowed to force a comparison out of an N-variant
        # file: that would compute a Stage B verdict from Stage A data — one replicate per row,
        # arms chosen on those same rows — which is precisely what the method forbids.
        notes.append(
            f"no paired comparison: {experiment_json} declares {len(result.variant_ids)} variant(s) "
            + f"({', '.join(result.variant_ids) or 'none'}) and no row of {suite_id!r} scored on both, "
            + "or the file predates per-replicate scores. The statistic fires only for EXACTLY two "
            + "variants, so gate one candidate at a time in its own round<N>-gate.yaml — re-passing "
            + "the triage experiment here produces no paired block at all."
        )
        return _verdict()

    if candidate_variant == comparison.vid_a:
        sign = 1.0
    elif candidate_variant == comparison.vid_b:
        sign = -1.0
    else:
        notes.append(
            f"candidate_variant={candidate_variant!r} is not one of the two variants the experiment "
            + f"compared ({comparison.vid_a!r}, {comparison.vid_b!r}), so no sign could be resolved and "
            + "no statistic is reported. Check the variant id against the experiment file."
        )
        return _verdict(rows_paired=comparison.task_count, rows_excluded=comparison.excluded_count)
    if incumbent_variant not in (comparison.vid_a, comparison.vid_b):
        # Fails CLOSED, like its candidate-side sibling above. It used to annotate and fall
        # through, which reported a real, significant difference against whichever arm the file
        # happened to carry — under a header naming the arm the caller asked for.
        notes.append(
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

    # Negating an interval reverses it, so re-order rather than reporting a "low" above its "high".
    bounds = sorted(b for b in (_signed(comparison.ci_low), _signed(comparison.ci_high)) if b is not None)
    if comparison.task_count < 2:
        notes.append(
            f"only {comparison.task_count} row(s) of {suite_id!r} scored on both arms — fewer than the 2 a "
            + "paired interval needs, so every statistic is reported as unavailable rather than fabricated."
        )
    # `paired_comparison` has one other way to return an all-`None` statistic on a sample big enough
    # for one: `paired_t_ci` declines on a NON-FINITE score. That cannot arrive through this
    # function, and the reason is worth recording rather than guarded against twice — pydantic's
    # JSON validator REJECTS `NaN` / `Infinity`, so such a file never parses and is already reported
    # by the read's own note above. A guard here would be an unreachable branch claiming otherwise.

    mean_diff = _signed(comparison.mean_diff)
    # Cohen's d carries the direction too, so it is signed with the rest.
    effect_size = _signed(comparison.effect_size)
    # `gate_refusal` may already carry the zero-row WIRING cause set above. Wiring outranks
    # variance — if the rows never loaded, whether their differences vary is moot — so the guard is
    # explicit rather than left to program order.
    if gate_refusal is None and mean_diff is not None and effect_size is None:
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
            gate_refusal = (
                f"the two arms produced an identical per-row score on every one of the "
                f"{comparison.task_count} paired row(s), so there is nothing for any test to "
                "separate — the paired difference is exactly zero with zero variance, and the "
                "paired t reports p = 1.0000 over a zero-width interval. That is a result about "
                "this candidate rather than about the suite: adding rows cannot change it. Check "
                "the candidate actually differs from the incumbent, and that both arms were wired "
                "to the snapshots you think they were."
            )
        else:
            gate_refusal = (
                f"the two arms differed by exactly {mean_diff:.3f} on every one of the "
                f"{comparison.task_count} paired row(s), so the paired differences carry zero "
                "variance. A paired t on a constant non-zero difference reports p = 0.0000 and a "
                "zero-width interval whatever the effect actually is — every promotion condition "
                "holds at once and none of them measured anything. This is not a result about the "
                "candidate. Add rows whose difference the two arms do NOT agree on, or add "
                "replicates so within-row spread can appear; a larger family or a smaller alpha "
                "cannot help."
            )
    if mde is not None and mean_diff is not None and abs(mean_diff) < mde:
        notes.append(
            f"the observed difference ({mean_diff:.3f}) is smaller than this suite's minimum detectable "
            + f"effect ({mde:.3f}) on weighted_score. An interval excluding zero is still reportable, but "
            + "do not present it as a comfortable win — the suite cannot resolve a difference this size."
        )

    return _verdict(
        rows_paired=comparison.task_count,
        rows_excluded=comparison.excluded_count,
        mean_diff=mean_diff,
        ci_low=bounds[0] if len(bounds) == 2 else None,
        ci_high=bounds[1] if len(bounds) == 2 else None,
        effect_size=effect_size,
        p_value=comparison.p_value,
    )


def holm_promote_execution(
    verdicts: list[ExecutionGateVerdict], alpha: float = DEFAULT_ALPHA
) -> list[ExecutionGateVerdict]:
    """Decide a whole execution-track family at once — the second, and only other, Holm call site.

    Gating candidates one at a time against the same incumbent on the same train rows IS a family,
    even though each gate is its own two-variant run directory. So the correction is applied once,
    across every verdict a round predeclared it would gate, exactly as :func:`holm_promote` does on
    the other track. Correcting per candidate would degenerate to an uncorrected ``p <= alpha``.

    **``promoted`` is Holm rejecting AND the difference favouring the candidate AND the interval
    excluding zero AND no refusal — nothing else.** Guardrails and integrity checks stay advisory in
    the model and gating in the render, which is how :func:`holm_promote` already treats guardrails:
    folding them into ``promoted`` would make :func:`render_execution_markdown`'s BLOCKED headline
    unreachable, and that headline is the thing that stops a reader promoting past a doubled row
    cost. The refusal conjunct is not of that kind — see below.

    A verdict with no ``p_value`` is outside the family and comes back ``promoted=False``.

    **There is a refusal state, and it is a different condition from the activation track's.** The
    paired *t* is continuous, so this track has no discreteness floor to refuse against — but it
    does have a degenerate sample: zero-variance paired differences (and, beside it, an arm that
    loaded no rows at all). ``execution_gate`` detects both where each is already computed and sets
    ``gate_refusal``; this function only READS it, forcing ``promoted=False`` and suppressing the
    negative-result notes, which would otherwise contradict the refusal headline.

    **A refused verdict is outside the family too, for the same reason a p-less one is**: it cannot
    be allowed to tighten the correction for the others. The zero-row cause is detected before
    ``experiment.json`` is even opened, so a refused verdict routinely carries a real p — and left
    in the vector it makes Holm's step-down break early on a candidate whose only problem is a
    *sibling* gate run's wiring fault. Conservative in direction, wrong in what it tells the reader.
    """
    family = [(i, v.p_value) for i, v in enumerate(verdicts) if v.p_value is not None and v.gate_refusal is None]
    rejections = holm_rejections([p for _i, p in family], alpha)
    rejected_at = {i for (i, _p), reject in zip(family, rejections, strict=True) if reject}

    decided: list[ExecutionGateVerdict] = []
    for i, verdict in enumerate(verdicts):
        notes = list(verdict.notes)
        if verdict.p_value is None:
            # Guarded like the three rungs below, and reachable: the zero-row refusal is set before
            # `experiment.json` is read, so "refused AND no p" is an ordinary state — this repo's
            # own mistyped-incumbent test produces it — and an unguarded note here prints an
            # ordinary negative result directly under a refusal headline.
            if verdict.gate_refusal is None:
                notes.append("not promoted: the sample could not support a p-value, so this arm is outside the family.")
            decided.append(verdict.model_copy(update={"promoted": False, "holm_alpha": alpha, "notes": notes}))
            continue

        favours_candidate = verdict.mean_diff is not None and verdict.mean_diff > 0.0
        excludes_zero = verdict.ci_low is not None and verdict.ci_low > 0.0
        # READ, never re-derived: `execution_gate` sets this where each condition is already
        # computed. It is LOAD-BEARING rather than belt-and-braces — a zero-variance verdict has
        # p = 0.0000 and a zero-width interval, so all three conjuncts above hold at once.
        refused = verdict.gate_refusal is not None
        promoted = i in rejected_at and favours_candidate and excludes_zero and not refused

        # Every negative-result rung is guarded, this ladder and the `p_value is None` one above:
        # a refused verdict whose difference happens to favour the incumbent would otherwise print
        # an ordinary negative-result note directly under a refusal headline — two contradictory
        # claims in one block.
        if not refused:
            if i in rejected_at and not favours_candidate:
                notes.append(
                    "not promoted: the paired difference favours the incumbent. (The sign is already "
                    + "resolved as candidate - incumbent, so this reads the way it looks.)"
                )
            elif i in rejected_at and not excludes_zero:
                notes.append(
                    "not promoted: the Holm-corrected test rejects but the confidence interval still "
                    + "contains zero, so the effect is not separated at the reported interval width."
                )
            elif i not in rejected_at:
                notes.append(
                    f"not promoted: p = {verdict.p_value:.4f} did not clear the Holm threshold for its rank in "
                    + f"a family of {len(family)} (alpha={alpha}). This is the ordinary negative result — the "
                    + "interval and the effect size above are what to report."
                )
        for check in (*verdict.integrity_checks, *verdict.guardrails):
            if not check.passed:
                notes.append(
                    f"{check.name} FAILED — this blocks the promotion even if the statistic separated. "
                    + "It is reported here and gated in the rendered block, not folded into `promoted`."
                )
        notes.append(f"Holm applied across a family of {len(family)} at alpha={alpha}.")
        decided.append(verdict.model_copy(update={"promoted": promoted, "holm_alpha": alpha, "notes": notes}))
    return decided


def render_execution_markdown(verdict: ExecutionGateVerdict) -> str:
    """The block the skill prints verbatim, mirroring :func:`render_markdown`'s headline precedence.

    Four headlines, in this precedence:

    - **UNDECIDED** — Holm has not run, so there is no decision to refuse or report.
    - **NOT A RESULT** — ``gate_refusal`` is set: either an arm loaded zero rows, or the paired
      differences carry zero variance. It outranks everything below because it says the sample
      decided nothing, and the message names which cause and its remedy. Deliberately NOT the
      activation track's CANNOT SEPARATE AT THIS SIZE, which reports a discreteness floor the
      paired *t* does not have — a shared string would make the two indistinguishable in a ledger
      read back weeks later.
    - **BLOCKED BY A GUARDRAIL** — the statistic separated but something non-primary failed. Below
      the refusal, since reading a guardrail presupposes a statistic that separated.
    - **PROMOTED / NOT PROMOTED** — the ordinary outcomes.

    ``UNDECIDED`` outranking the refusal is right — a verdict Holm never saw has no decision to
    refuse — but the refusal's TEXT must still reach the reader, so it is printed on its own line
    whenever the headline could not carry it. Without that, a pre-Holm block over a mis-wired arm
    renders a confident interval and four green checks with nothing anywhere saying the rows are
    not there: the message used to live in ``notes``, which every path prints, and moving it to a
    headline-only channel is what would have lost it.
    """
    failed = [check.name for check in (*verdict.integrity_checks, *verdict.guardrails) if not check.passed]
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote_execution has not been applied, so this verdict decides nothing"
    elif verdict.gate_refusal is not None:
        headline = f"NOT A RESULT — {verdict.gate_refusal}"
    elif verdict.promoted and failed:
        headline = (
            "BLOCKED BY A GUARDRAIL — the paired comparison separated, but "
            + f"{', '.join(failed)} failed. Do not promote on this block."
        )
    else:
        headline = "PROMOTED" if verdict.promoted else "NOT PROMOTED"

    lines = [
        f"### Execution gate — `{verdict.candidate_variant}` vs `{verdict.incumbent_variant}`",
        "",
        f"**{headline}**",
        "",
    ]
    # Exactly the one path where the headline did not carry it — so the message appears once, never
    # twice, which is the rule the refusal replaced its own note under.
    if verdict.gate_refusal is not None and verdict.promoted is None:
        lines += [f"**NOT A RESULT:** {verdict.gate_refusal}", ""]
    lines += [
        f"- Suite `{verdict.suite_id}`, per-row `weighted_score` through the reporter's paired comparison",
        f"- Rows paired: {verdict.rows_paired} · excluded: {verdict.rows_excluded}",
        (
            "- Paired mean difference (candidate - incumbent, sign resolved by the tool): "
            + f"{_fmt(verdict.mean_diff)} {verdict.confidence:.0%} CI "
            + f"[{_fmt(verdict.ci_low)}, {_fmt(verdict.ci_high)}]"
        ),
        f"- Cohen's d: {_fmt(verdict.effect_size)} · p = {_fmt(verdict.p_value, '.4f')}",
        f"- Holm alpha: {_fmt(verdict.holm_alpha, '.3f')}",
        f"- Interval excludes zero: {verdict.ci_low is not None and verdict.ci_low > 0.0}",
        f"- Minimum detectable effect (weighted_score): {_fmt(verdict.mde)}",
    ]
    lines += _render_checks("Integrity checks", verdict.integrity_checks)
    lines += _render_checks("Guardrails", verdict.guardrails)
    if verdict.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {note}" for note in verdict.notes]
    return "\n".join(lines)


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "—" if value is None else f"{value:{spec}}"


def _render_checks(title: str, checks: list[GuardrailCheck]) -> list[str]:
    if not checks:
        return []
    lines = [f"- **{title}:**"]
    for check in checks:
        state = "PASS" if check.passed else "FAIL"
        detail = f"{_fmt(check.incumbent)} -> {_fmt(check.candidate)}"
        # The interval is WHY the check did or did not fire — a verdict without it is unauditable.
        interval = (
            f", diff CI [{_fmt(check.ci_low)}, {_fmt(check.ci_high)}] vs floor {check.tolerance:.2f} x incumbent"
            if check.ci_low is not None
            else ""
        )
        # A second reading where the check has one. Omitted rather than printed as `—`: every
        # check without a rate would otherwise carry a column that means nothing for it.
        rate = f", rate {check.rate:.3f}" if check.rate is not None else ""
        note = f" — {check.note}" if check.note else ""
        lines.append(f"  - {state} · {check.name}: {detail}{interval}{rate}{note}")
    return lines


def render_markdown(verdict: ActivationGateVerdict) -> str:
    """The block the skill prints verbatim, numbers and all.

    Four headlines, in this precedence, and each is a different claim:

    - **UNDECIDED** — ``promoted`` is ``None``, so :func:`holm_promote` never ran. It outranks
      everything below because a verdict Holm never saw has no threshold to be refused against.
      Silently reading ``None`` as "not promoted" would let a forgotten call look like an honest
      negative result — the failure this whole gate exists to prevent.
    - **CANNOT SEPARATE AT THIS SIZE** — ``gate_refusal`` is set: the suite's discreteness floor
      exceeds the Holm threshold, so no candidate could promote however good it is. It outranks
      NOT PROMOTED because it is a statement about the suite, not about this candidate.
    - **BLOCKED BY A GUARDRAIL** — the statistic separated but a guardrail failed. Below the
      refusal, since reading a guardrail presupposes a statistic that separated.
    - **PROMOTED / NOT PROMOTED** — the ordinary outcomes.
    """
    failed_guardrails = [check.name for check in verdict.guardrails if not check.passed]
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote has not been applied, so this verdict decides nothing"
    elif verdict.gate_refusal is not None:
        headline = f"CANNOT SEPARATE AT THIS SIZE — {verdict.gate_refusal}"
    elif verdict.promoted and failed_guardrails:
        # `promoted` here already includes the sibling checks; what it does NOT include is the
        # cost/latency guardrails, which gate in the procedure. A
        # bare "PROMOTED" over a failing guardrail is the misread this line exists to prevent —
        # the reader prints the block and ships a candidate that doubled what a row costs.
        headline = (
            "BLOCKED BY A GUARDRAIL — the primary comparison separated, but "
            + f"{', '.join(failed_guardrails)} failed. Do not promote on this block."
        )
    else:
        headline = "PROMOTED" if verdict.promoted else "NOT PROMOTED"

    lines = [
        f"### Activation gate — `{verdict.candidate_variant}` vs `{verdict.incumbent_variant}`",
        "",
        f"**{headline}**",
        "",
        f"- Suite `{verdict.suite_id}`, criterion index {verdict.criterion_index} (position in `success_criteria`)",
        # The discordant count sits beside the paired one because it is what `p_floor` below is
        # computed from — a reader handed a floor without it cannot see the quantity that moves it.
        (
            f"- Rows paired: {verdict.rows_paired} · discordant: {_fmt(verdict.n_discordant, 'd')} "
            + f"· excluded: {verdict.rows_excluded}"
        ),
        f"- f1.yes: incumbent {_fmt(verdict.incumbent_f1)} -> candidate {_fmt(verdict.candidate_f1)}",
        (
            f"- Paired cluster bootstrap (candidate - incumbent): {_fmt(verdict.mean_diff)} "
            + f"{verdict.confidence:.0%} CI [{_fmt(verdict.ci_low)}, {_fmt(verdict.ci_high)}], "
            + f"p = {_fmt(verdict.p_value, '.4f')} over {verdict.n_resamples} draws"
        ),
        # TWO floors, and the second is the one that decides. The estimator's is a property of the
        # draw count; this suite's is a property of its discordant rows, sits above it, and is what
        # holm_promote compares against the corrected threshold. Neither formula is spelled here —
        # `bootstrap_p_floor` owns one and `_discreteness_floor` the other, and a formula retyped
        # into a display string is a second declaration that cannot be kept honest.
        (
            f"- p floors: estimator {bootstrap_p_floor(verdict.n_resamples):.4f} "
            + f"at {verdict.n_resamples} draws · this suite {_fmt(verdict.p_floor, '.4f')}"
        ),
        f"- Holm alpha: {_fmt(verdict.holm_alpha, '.3f')}",
        f"- Interval excludes zero: {verdict.ci_low is not None and verdict.ci_low > 0.0}",
        f"- Range non-overlap (DIAGNOSTIC, not the gate): {verdict.range_non_overlap}",
        f"- Minimum detectable effect: {_fmt(verdict.mde)}",
    ]
    lines += _render_checks("Sibling checks", verdict.sibling_checks)
    lines += _render_checks("Guardrails", verdict.guardrails)
    if verdict.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {note}" for note in verdict.notes]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-row score vectors, the row x candidate matrix, and the Pareto front
# ---------------------------------------------------------------------------


def _row_score(result: EvaluationResult, criterion_index: int | None) -> float | None:
    """The row's score for one arm: the criterion's score, or the row's weighted score.

    ``None`` when the row produced no criterion results at all. That case matters on the execution
    track specifically: ``calculate_weighted_score`` sets ``weighted_score`` to 0.0 for an empty
    result list, so an errored or timed-out row arrives as a *scored zero* rather than a hole — and
    the arm is then discarded from the Pareto front for having crashed, with no `—` in the matrix
    to show it. A hole is not a failure, and this is where that distinction is enforced.
    """
    if not result.success_criteria_results:
        return None
    if criterion_index is None:
        return result.weighted_score
    if criterion_index >= len(result.success_criteria_results):
        return None
    return result.success_criteria_results[criterion_index].score


def arm_row_scores(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None = None,
) -> list[ArmRowScores]:
    """Each arm's per-row score vector, averaged across replicates.

    ``run_dirs`` is a LIST, like every other function here, because Stage A and Stage B are
    separate invocations — a single-run-dir signature would silently compute the front off one
    replicate. Replicates reduce to one number per (row, arm) by the **mean**, the same reduction
    ``paired_comparison`` applies before pairing, so the two surfaces agree about what a row scored.

    ``criterion_index=None`` reads the row's ``weighted_score`` (the execution track); an index
    reads that criterion's score (the activation track). A row an arm produced no score for is
    left ABSENT from the vector rather than recorded as 0.0 — see :func:`pareto_front`.
    """
    arms: list[ArmRowScores] = []
    for variant_id in variant_ids:
        rows = _pool([load_suite_rows(d, variant_id, suite_id) for d in run_dirs])
        scores: dict[str, float] = {}
        for row_id, results in sorted(rows.items()):
            values = [v for r in results if (v := _row_score(r, criterion_index)) is not None]
            if values:
                scores[row_id] = mean(values)
        arms.append(ArmRowScores(variant_id=variant_id, row_scores=scores))
    return arms


def candidate_leaks(
    candidate_text: str,
    baseline_text: str,
    rows: Sequence[TaskDefinition],
) -> list[str]:
    """Train-row content a candidate reproduces verbatim AND its baseline does not.

    A preflight, not part of any verdict: it needs no runs, so it is read at proposal time, before
    Stage A is paid for. Distinct from ``regression_check`` beside it, which asks whether an arm
    *re-lost a measured row* and therefore cannot be answered without run results.

    **A DIFF, not an absolute scan, and the difference is what makes it usable.** Measured against
    this repo's own ``tasks/skills/ci-outcome.yaml`` on its TRAIN split, an absolute scan flags the
    shipped ``ci`` skill on five strings — ``minimum-task-score``, ``persist-credentials: false``
    and three more — none of which is memorization: that body legitimately documents the output
    contract its suite grades. A checker that fires on the shipped skill on its first run is one
    users learn to ignore. What is worth flagging is what a candidate NEWLY absorbs.

    ``baseline_text`` is **the text this candidate was derived from**, which is not always the
    incumbent: from round 2 a search-loop candidate is built on the *lineage head*, and diffing
    that against the incumbent would re-report every span the head added, every round. Pass the
    arm the candidate was actually edited from.

    **Four boundaries, stated so an empty result is not mistaken for a proof:**

    - It catches the VERBATIM form, as CE036 states of its own. A candidate that describes a train
      row's content in other words is a semantic leak and needs a reader. (Matching is
      case-insensitive on both sides, so casing alone does not evade it.)
    - Containment is a **substring** test in both directions, so a graded value can be masked by an
      unrelated baseline substring that happens to contain it, and flagged for a subword
      occurrence. The :data:`~coder_eval.leak_detection.LEAK_MIN_CHARS` floor makes both unlikely
      rather than impossible.
    - **A span already in the baseline is invisible from here on.** That is right while the
      baseline is the user's shipped skill, which is the measured case above — but from round 2 the
      baseline is itself a former candidate, so a memorized span that rode into a promotion
      alongside a genuine improvement is never flagged again. The proposer-side rule in
      ``reference/proposal-prompt.md`` is what covers that; this function cannot.
    - **The gold solution is out of reach.** Only ``row.success_criteria`` is scanned.
      ``TaskDefinition.reference`` — the reference solution ``reference_comparison`` / ``llm_judge``
      / ``agent_judge`` score against — is a task-level field, and it may name a file or a whole
      directory rather than carry its text, so scanning it would mean reading the filesystem from
      what is otherwise a pure function. That matters more than it looks: ``proposal-prompt.md``
      tells the proposer to *study* the reference, and calls copying it "especially tempting"
      because the content is known-correct. This checker cannot see that copy. A reader has to.

    ``rows`` are the EXPANDED row-tasks of the TRAIN split only — passing the whole suite would
    flag content drawn from rows the candidate is entitled to be fitted to. (The five-string
    figure above is the train split's; the whole suite gives seven.)

    Findings are de-duplicated, preserving order. A suite may assert the same string twice on a
    row — this repo's own does — and repeating the line says nothing the first one did not, in a
    check whose entire design rationale is not firing more than it has to.
    """
    candidate = candidate_text.lower()
    baseline = baseline_text.lower()
    findings: list[str] = []
    for row in rows:
        for criterion in row.success_criteria:
            for value in graded_strings(criterion, drop_type=True):
                lowered = value.lower()
                if lowered in candidate and lowered not in baseline:
                    findings.append(f"{row.task_id}: candidate adds {value!r} ({criterion.type})")
    return list(dict.fromkeys(findings))


class SearchComparison(NamedTuple):
    """The search loop's accept/revert decision for one round's single candidate.

    A NamedTuple beside :class:`CostQualityPoint`, and for the same reason: computed and rendered,
    never persisted. What IS persisted is the outcome — ``RoundScores.lineage_head`` — and that is
    a model.

    ``beats`` and ``accepted`` are deliberately two fields rather than one. ``beats`` is the score
    comparison alone; ``accepted`` is that AND nothing blocking it. Collapsing them would make a
    corpus regression indistinguishable from a candidate that simply scored worse, and those two
    call for opposite next actions — one is "look at the row and decide", the other is "write the
    next hypothesis".
    """

    beats: bool
    accepted: bool
    head_score: float | None
    candidate_score: float | None
    shared_rows: tuple[str, ...]
    holes: tuple[str, ...]
    regressions: tuple[tuple[RegressionRow, float | None], ...]
    blocker: str | None


def lineage_head_scores(measurements: OptimizeMeasurements) -> ArmRowScores | None:
    """The arm the most recent round carried forward, or ``None`` when no round named one.

    The highest ``round`` that recorded a ``lineage_head``, **not** the last entry in the list:
    ``record_round_scores`` replaces per round, so list order is a write-order artefact while
    ``round`` is the real sequence. A later round that accepted nothing is skipped rather than
    blanking the lineage — a quiet round leaves the head where it was.

    ``RoundScores``' own validator guarantees the named arm is present with a non-empty vector, so
    the lookup below cannot raise.
    """
    named = [r for r in measurements.round_scores if r.lineage_head is not None]
    if not named:
        return None
    last = max(named, key=lambda r: r.round)
    return next(a for a in last.arm_row_scores if a.variant_id == last.lineage_head)


def search_compare(
    head: ArmRowScores,
    candidate: ArmRowScores,
    *,
    corpus: Sequence[RegressionRow] = (),
    threshold: float = 1.0,
) -> SearchComparison:
    """Whether the search loop should carry ``candidate`` forward in place of ``head``.

    **Not a gate.** The two means come from different invocations, unpaired, unreplicated and
    uncorrected — the arithmetic the promotion gate exists to distrust. A ``True`` here is a
    hypothesis to gate, never a result, and nothing in this function promotes anything.

    It exists as a function rather than as arithmetic in the skill's prose because each guard
    below only works if it is applied, and the previous home for them was a markdown block an
    agent copies and adapts:

    - **The comparison runs over the rows BOTH arms scored, and nothing else.** ``head``'s vector
      was recorded in an earlier round and ``candidate``'s comes from the run just paid for, so
      nothing guarantees they cover the same rows — and every way they diverge favours the
      candidate.
    - **No overlap at all is reported before holes are**, because it is a *wiring* fault (an
      unpinned ``dataset.sample_seed`` draws a different sample across invocations) and calling it
      a hole sends the reader hunting a flaky row.
    - **A hole refuses rather than averaging around it.** A candidate that errored on the hardest
      rows scores a higher mean over the survivors; that is the rule :func:`_dominates` already
      applies to the row matrix. A refused comparison reports ``None`` for both scores rather than
      a number nobody should read.
    - **A corpus regression blocks an otherwise-winning candidate.** A search accept advances the
      lineage, so a row an earlier promotion was built on would be re-lost and carried forward
      until the next multi-arm round noticed. An aggregate cannot show that — the whole premise of
      the corpus — and the check is free here because ``regression_check`` takes exactly the arm
      this function already has.

    A tie does not win: ``beats`` requires strictly greater. Advancing the head on a tie moves the
    bar every later round is judged against, on an accident.

    ``corpus`` and ``threshold`` are forwarded to :func:`regression_check`; the default of 1.0
    treats any partial score as a loss, which is right for the binary activation criterion the
    corpus is usually written from.
    """
    shared = tuple(sorted(set(head.row_scores) & set(candidate.row_scores)))
    holes = tuple(sorted(set(head.row_scores) - set(candidate.row_scores)))

    def _refused(blocker: str) -> SearchComparison:
        return SearchComparison(False, False, None, None, shared, holes, (), blocker)

    if not head.row_scores:
        return _refused(
            "the lineage head scored no rows, so there is no bar to beat — record a head from a "
            + "round that measured something"
        )
    if not shared:
        return _refused(
            "the two rounds share no rows, so there is nothing to compare — a wiring fault rather "
            + "than a result. Pin `dataset.sample_seed` if the suite samples, and check both arms "
            + "mounted the snapshot you think they did."
        )
    if holes:
        return _refused(
            f"the candidate produced no score for {list(holes)}, which the head scored. A hole is "
            + "not a win: averaging over the survivors would reward the arm that failed on them. "
            + "Re-run before reading this."
        )

    head_score = mean([head.row_scores[r] for r in shared])
    candidate_score = mean([candidate.row_scores[r] for r in shared])
    beats = candidate_score > head_score

    regressions = tuple(regression_check(list(corpus), candidate, threshold=threshold)) if beats else ()
    blocker = None
    if regressions:
        lost = ", ".join(f"{row.row_id} ({row.reason})" for row, _ in regressions)
        blocker = (
            f"the candidate's train score improves but it re-loses {lost} — rows an earlier "
            + "promotion was built on. A search accept advances the lineage, so accepting this "
            + "carries the regression forward until a multi-arm round notices."
        )
    return SearchComparison(
        beats=beats,
        accepted=beats and blocker is None,
        head_score=head_score,
        candidate_score=candidate_score,
        shared_rows=shared,
        holes=holes,
        regressions=regressions,
        blocker=blocker,
    )


def render_search_comparison(comparison: SearchComparison) -> str:
    """The search comparison as a markdown block, for the ledger.

    Says *why* on every path, and says what an accept is not — the block is read back weeks later
    beside gate verdicts that look similar and mean something much stronger.

    Both scores are ``float | None`` on the model, so they print through :func:`_fmt` — the
    module's one declaration of how it renders an optional float — rather than through a bare
    ``:.3f``, which raises on ``None``. Every ``None`` path here is currently a ``_refused`` one
    that returns above, so the ``—`` is not reachable through :func:`search_compare` today; the
    formatting is not conditional on that staying true, because the function is public and the
    alternative is a ``TypeError`` out of the skill's inline snippet.
    """
    if comparison.blocker is not None:
        headline = "DO NOT ACCEPT" if comparison.beats else "CANNOT COMPARE"
        lines = [f"### Search round — {headline}", "", comparison.blocker]
        if comparison.beats:
            lines += [
                "",
                f"Train score {_fmt(comparison.candidate_score)} against the head's {_fmt(comparison.head_score)}.",
            ]
        return "\n".join(lines)

    verdict = "ACCEPT into the lineage" if comparison.accepted else "REVERT — the head stands"
    return "\n".join(
        [
            f"### Search round — {verdict}",
            "",
            f"- Candidate: **{_fmt(comparison.candidate_score)}**",
            f"- Lineage head: {_fmt(comparison.head_score)}",
            f"- Compared over {len(comparison.shared_rows)} shared row(s).",
            "",
            "Unpaired, unreplicated and uncorrected across invocations, so **a search accept is "
            + "not a promotion**: it advances the lineage head only. The incumbent moves at Stage B "
            + "plus Stage C and nowhere else.",
        ]
    )


def _finite_scores(arm: ArmRowScores) -> dict[str, float]:
    """An arm's row vector with non-finite cells removed — a NaN is treated as ABSENT.

    The same guard :func:`instance_best_front` and :func:`cost_quality_front` already apply, in the
    one place the coverage front was missing it. Every ``>=`` and ``>`` against NaN is False, so a
    NaN cell makes its arm undominatable by anyone AND unable to dominate anyone — it takes the
    front by incomparability, rendered in bold beside arms that earned it. Treating it as absent
    instead routes it through the coverage rule, which is the answer already agreed for a hole.

    Nothing produces a non-finite score today (``_row_score`` returns means of scores bounded
    [0, 1]), which is exactly why it would be silent.
    """
    return {row_id: value for row_id, value in arm.row_scores.items() if math.isfinite(value)}


def _dominates(a: ArmRowScores, b: ArmRowScores) -> bool:
    """True when ``a`` covers every row ``b`` scored, matches it on all of them, and beats it on one.

    Holes are handled by requiring **coverage**, and both halves of that matter:

    - A missing cell is never read as 0.0. That would fabricate domination against the arm that
      happens to have the hole, which is the opposite of what a hole means.
    - An arm cannot dominate on the rows it happens to share while being ABSENT from a row the
      other arm won. It has no evidence there, and "at least as good everywhere" is a claim about
      everywhere the other arm was measured — so it is not entitled to make it.

    A non-finite cell counts as a hole rather than as a value — see :func:`_finite_scores`.
    """
    a_scores, b_scores = _finite_scores(a), _finite_scores(b)
    scored_by_b = sorted(b_scores)
    if not scored_by_b or not set(scored_by_b) <= set(a_scores):
        return False
    return all(a_scores[r] >= b_scores[r] for r in scored_by_b) and any(a_scores[r] > b_scores[r] for r in scored_by_b)


def pareto_front(arms: list[ArmRowScores]) -> list[str]:
    """Variant ids no other arm dominates on the row vector.

    Identical arms all stay on the front (nothing is strictly better), which is itself the finding:
    the candidates did not differ anywhere the suite could see. A single arm is its own front.

    An arm that scored **no** rows is excluded rather than undominatable. Nothing can cover an
    empty vector, so the domination rule alone would put a candidate that crashed on every row on
    the front — rendered indistinguishably from one that won something nobody else did. An arm
    whose every cell is non-finite is excluded by the same rule, since :func:`_finite_scores`
    leaves it with an empty vector.
    """
    scored = [arm for arm in arms if _finite_scores(arm)]
    return [
        arm.variant_id
        for i, arm in enumerate(scored)
        if not any(_dominates(other, arm) for j, other in enumerate(scored) if i != j)
    ]


def instance_best_front(arms: list[ArmRowScores]) -> list[str]:
    """Variant ids achieving the highest score on at least one row — GEPA's frontier definition.

    A DIFFERENT set from :func:`pareto_front`, and the difference is the point. Ours is
    "not dominated on the row vector", which is the right rule for **discarding**: an arm off it
    was matched or beaten on every row it was measured on. GEPA's is the right rule for **merging**,
    because
    it deliberately retains an arm that wins exactly one row — precisely the raw material a merge
    candidate is built from, and precisely what a coverage rule can drop.

    Neither contains the other. Measured on a four-arm fixture:
    ``A={r1:0.5, r2:0.5}``, ``B={r1:1.0, r2:0.4}``, ``C={r1:0.4, r2:1.0}``, ``D={r1:1.0, r2:0.3}``
    gives coverage ``[A, B, C]`` and instance-best ``[B, C, D]``. ``A`` is dominated by nobody yet
    wins nothing; ``D`` ties a row's maximum yet is dominated outright by ``B``.

    The maximum on a row is taken over the arms that SCORED it — a hole is never a zero, exactly as
    in :func:`_dominates` — so an arm that alone measured a row is trivially the best on it.

    Ties all qualify: an arm equal to the best on a row achieved the best on it, which mirrors
    :func:`pareto_front` keeping identical arms. Exact ``==`` is the right comparison and a
    tolerance would silently widen the front — every score comes from the same ``mean(values)``
    reduction over the same replicates, so equal scores are equal for a reason, not by luck.

    An arm that scored no rows is excluded for the same reason it is there: nothing about an empty
    vector is a win. Returns in input order, matching :func:`pareto_front`.
    """
    scored = [arm for arm in arms if arm.row_scores]
    best: dict[str, float] = {}
    for arm in scored:
        for row_id, value in arm.row_scores.items():
            # Non-finite values are skipped when SEEDING the maximum. `value > nan` is False, so a
            # single NaN landing in a row would pin that row's maximum at NaN forever — and then
            # `v == best[r]` is False for every arm, dropping not just the NaN arm but the arm that
            # genuinely won the row. Nothing produces one today (`_row_score` returns means of
            # scores bounded [0, 1]), which is exactly why it would be silent.
            if math.isfinite(value) and (row_id not in best or value > best[row_id]):
                best[row_id] = value
    return [arm.variant_id for arm in scored if any(v == best.get(r) for r, v in arm.row_scores.items())]


def render_row_matrix(arms: list[ArmRowScores], pareto: list[str], *, instance_best: list[str] | None = None) -> str:
    """The row x candidate table, with the Pareto set marked and the holes made visible.

    ``instance_best`` is keyword-only and optional so the existing two-positional-argument form
    keeps working byte-for-byte. When given, the block names both fronts AND the arms they disagree
    about — a reader shown two lists learns nothing; the diff is the finding.
    """
    if not arms:
        return "_No arms to compare._"

    row_ids = sorted({rid for arm in arms for rid in arm.row_scores})
    header = " | ".join(f"**{a.variant_id}**" if a.variant_id in pareto else a.variant_id for a in arms)
    lines = [
        f"| row | {header} |",
        "|" + "---|" * (len(arms) + 1),
    ]
    for row_id in row_ids:
        cells = " | ".join(f"{a.row_scores[row_id]:.3f}" if row_id in a.row_scores else "—" for a in arms)
        lines.append(f"| {row_id} | {cells} |")

    lines.append("")
    lines.append(f"Pareto front (**bold**): {', '.join(pareto) if pareto else 'none'}")
    if instance_best is not None:
        listed = ", ".join(instance_best) if instance_best else "none"
        lines.append(f"Instance-best front (GEPA's, the merge shortlist): {listed}")
        only_coverage = [v for v in pareto if v not in instance_best]
        only_instance = [v for v in instance_best if v not in pareto]
        if only_coverage or only_instance:
            parts = []
            if only_coverage:
                parts.append(f"on coverage without winning any row: {', '.join(only_coverage)}")
            if only_instance:
                parts.append(f"wins a row despite being dominated overall: {', '.join(only_instance)}")
            lines.append(
                "The two fronts disagree, which is the interesting case rather than an inconsistency: "
                + "; ".join(parts)
                + ". Coverage is the set to DISCARD from; instance-best is the set to MERGE from."
            )
        elif pareto or instance_best:
            # Only when there is something to agree ABOUT. With both fronts empty every arm
            # crashed, and "both fronts agree" would read as a result immediately above the line
            # saying it is a wiring problem.
            lines.append("Both fronts agree on these arms.")

    unscored = [a.variant_id for a in arms if not a.row_scores]
    if unscored:
        lines.append(
            f"Arms that scored no rows at all and are therefore NOT on the front: {', '.join(unscored)}. "
            + "That is a wiring or crash problem, not a result."
        )

    holes = [rid for rid in row_ids if any(rid not in a.row_scores for a in arms)]
    if holes:
        lines.append(
            "Rows missing from at least one arm, shown as — and excluded from the domination "
            + f"comparison rather than counted as 0.0: {', '.join(holes)}"
        )
    # Every arm that MEASURED the row scored zero — an arm's hole is not a zero here either.
    floored = [rid for rid in row_ids if all(a.row_scores[rid] == 0.0 for a in arms if rid in a.row_scores)]
    if floored:
        lines.append(
            f"Rows no arm scored above zero: {', '.join(floored)}. These contribute nothing to the "
            + "front — usually a broken row or an unmet fixture precondition rather than N bad candidates."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Quality x cost — a second axis of the shortlist, never a second gate
# ---------------------------------------------------------------------------


# The one declaration of what this front is and is not. Rendered by render_cost_quality and
# asserted against both prose surfaces by a sensor that IMPORTS it — the same shape as the
# MATERIALITY_FLOOR sensor, so the claim cannot exist in three files at three vintages.
COST_FRONT_ADVISORY = (
    "This front is advisory. Promotion is unchanged: the primary statistic must separate and "
    "every guardrail must hold, so a cheaper arm here is a trade to offer the user, never a "
    "promotion this tool makes. Read it with the arms you are actually choosing between: any arm "
    "that is cheap because it does less — an emptied-body control, say — sits on this front by "
    "construction, since nothing dominates an arm nobody is trying to beat on cost."
)


class CostQualityPoint(NamedTuple):
    """One arm's position on the quality x cost plane.

    A NamedTuple rather than a Pydantic model because it is computed and rendered, never
    persisted — the same call the module already makes for ``ArmRowScores`` in the other direction
    (that one IS persisted, in ``RoundScores``, which is why it is a model).
    """

    variant_id: str
    score: float | None  # mean of the arm's per-row scores; None when it scored nothing
    cost_per_row: float | None  # median of the arm's per-row mean cost; None when nothing recorded
    # The rows BOTH coordinates are reduced over — the identities, not just how many. `_dominates`
    # gates domination on set COVERAGE, and a count cannot express that: two arms measured on four
    # rows each, disjoint, would each look entitled to dominate the other.
    row_ids: frozenset[str] = frozenset()

    @property
    def n_rows(self) -> int:
        """How many rows this arm was measured on — what the render shows."""
        return len(self.row_ids)


def cost_quality_points(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None = None,
) -> list[CostQualityPoint]:
    """Each arm's mean row score against its median per-row cost.

    Quality reuses :func:`arm_row_scores`, so this view and the row matrix cannot disagree about
    what a row scored. Cost reuses :func:`_row_cost_levels`, so it and the guardrail cannot
    disagree about what a row cost.

    **BOTH coordinates are reduced over the same rows: the ones the arm actually scored.** Cost is
    read only for those rows, not for every row the arm has on disk. Without that restriction an
    arm that CRASHED most of its rows is described by two different samples — its quality averaged
    over the handful it completed, its cost over all of them, holes included, because a crashed row
    still records a `total_cost_usd`. Reproduced before the fix: an arm completing 1 of 6 rows at a
    perfect score took the whole front and knocked the incumbent off it, rendered as two clean
    numbers with nothing to show the other five rows were missing. ``n_rows`` reports the count so
    the render can say which arms are standing on less evidence, exactly as `_dominates` requires
    row coverage and `render_row_matrix` prints `—`.

    ``cost_per_row`` is the **median** over those rows — the same reduction
    ``GuardrailCheck.incumbent`` reports, so the two surfaces print the same number. Parity is
    close but not exact by construction, and the two reasons are worth knowing: the guardrail
    balances replicate counts *pairwise between two arms* before reducing, and it reduces over the
    rows BOTH arms scored (or the explicit ``row_ids`` the gate hands it) rather than over one
    arm's own. An N-arm view can do neither. Where every arm scored every row with equal replicate
    counts — the ordinary case — they agree exactly.

    ``criterion_index=None`` reads each row's ``weighted_score`` (the execution track); an index
    reads that criterion's score (the activation track). The same switch ``arm_row_scores``
    already has, rather than a second track parameter.
    """
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    points: list[CostQualityPoint] = []
    for arm in arms:
        rows = load_arm_rows(run_dirs, arm.variant_id, suite_id)
        scored_ids = sorted(arm.row_scores)
        levels = _row_cost_levels([_row_costs(rows.get(rid, [])) for rid in scored_ids])
        points.append(
            CostQualityPoint(
                variant_id=arm.variant_id,
                score=mean(list(arm.row_scores.values())) if arm.row_scores else None,
                cost_per_row=_median(levels),
                row_ids=frozenset(scored_ids),
            )
        )
    return points


def cost_quality_front(points: list[CostQualityPoint]) -> list[str]:
    """Variant ids no other arm beats on BOTH quality and cost.

    An arm is dominated when another scores at least as well AND costs at most as much, with at
    least one of the two strict — **and covers every row it was measured on.** Ties therefore all
    stay, matching :func:`pareto_front`.

    That last clause is the same coverage precondition :func:`_dominates` applies to the row
    vector, and it is load-bearing for the same reason. Without it an arm that CRASHED on five of
    six rows and scored 1.0 on the sixth dominates an incumbent that scored 0.9 on all six at the
    same cost — measured, and it knocked the incumbent off the front entirely. An arm standing on
    less evidence is not entitled to a claim about "everywhere"; it stays on the front itself,
    where :func:`render_cost_quality` names its row count, rather than displacing an arm that was
    actually measured.

    Coverage is a SET test, not a count. Two arms measured on four rows each, on disjoint rows,
    each have "at least as many" as the other and would each be entitled to dominate — while
    neither has any evidence about where the other was measured. Comparing the row ids is what
    makes the aggregate rule agree with the row-vector one it is modelled on.

    An arm missing either coordinate is **excluded**, mirroring how ``pareto_front`` treats an arm
    with an empty vector: a point with no cost is not a free point, it is an unmeasured one, and
    putting it on the front would render it indistinguishable from the genuinely cheapest arm.
    :func:`render_cost_quality` names the excluded arms rather than dropping them silently.

    A **zero** cost is a real coordinate, not a missing measurement — a free model is legitimately
    the cheapest arm there is. So the test is ``is not None``, never truthiness, the same rule
    ``register_pricing`` states for an all-zero rate.

    A **non-finite** coordinate is excluded for the opposite reason: every ``>=`` / ``<=`` against
    NaN is False, so a NaN arm is undominatable and would render in bold as a live trade.

    **All three fronts guard non-finite values**, and now agree about them: this one excludes the
    arm, :func:`instance_best_front` skips the cell when seeding a row's maximum, and
    :func:`pareto_front` treats it as a hole via :func:`_finite_scores`. The mechanisms differ
    because the three answer different questions; the outcome — a non-finite cell never wins
    anything and never makes its arm undominatable — is the same, and a parametrized test asserts
    it across all three rather than leaving this sentence to be believed.
    """
    # Narrowed to plain floats up front rather than suppressing the comparison's type error: the
    # filter IS the exclusion rule, so making it produce a non-optional shape is what keeps the
    # rule and the types saying the same thing.
    measured = [
        (p.variant_id, p.score, p.cost_per_row, p.row_ids)
        for p in points
        if p.score is not None
        and p.cost_per_row is not None
        and math.isfinite(p.score)
        and math.isfinite(p.cost_per_row)
    ]
    return [
        variant_id
        for i, (variant_id, score, cost, row_ids) in enumerate(measured)
        if not any(
            row_ids <= other_ids
            and other_score >= score
            and other_cost <= cost
            and (other_score > score or other_cost < cost)
            for j, (_o_id, other_score, other_cost, other_ids) in enumerate(measured)
            if i != j
        )
    ]


def render_cost_quality(points: list[CostQualityPoint], front: list[str]) -> str:
    """The quality x cost table, with the front in bold and the advisory rendered from its constant."""
    if not points:
        return "_No arms to compare._"

    lines = [
        "| arm | rows | mean row score | median cost/row (USD) |",
        "|---|---|---|---|",
    ]
    for point in points:
        name = f"**{point.variant_id}**" if point.variant_id in front else point.variant_id
        lines.append(f"| {name} | {point.n_rows} | {_fmt(point.score)} | {_fmt(point.cost_per_row, '.4f')} |")

    lines.append("")
    lines.append(f"Cost/quality front (**bold**): {', '.join(front) if front else 'none'}")

    # An arm scoring fewer rows than the best-covered one is standing on less evidence, and BOTH of
    # its coordinates are averages over that smaller sample. Named for the same reason
    # `render_row_matrix` prints `—`: a partly-crashed arm can look like a clean trade otherwise.
    covered = max((p.n_rows for p in points), default=0)
    thin = [f"{p.variant_id} ({p.n_rows}/{covered})" for p in points if 0 < p.n_rows < covered]
    if thin:
        lines.append(
            f"Arms scored on fewer rows than the best-covered arm: {', '.join(thin)}. Both of their "
            + "coordinates are averages over that smaller sample, so a favourable position here may "
            + "be the missing rows rather than a real trade — check the row matrix before reading it."
        )

    excluded = [p.variant_id for p in points if p.score is None or p.cost_per_row is None]
    if excluded:
        lines.append(
            f"Arms missing a coordinate and therefore NOT on the front: {', '.join(excluded)}. "
            + "An unmeasured cost is not a free one, so they are excluded rather than placed at zero."
        )
    lines.append("")
    lines.append(COST_FRONT_ADVISORY)
    return "\n".join(lines)
