"""The activation track's promotion gate, computed from finalized run directories.

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
import os
import statistics as _stats
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from coder_eval.criteria._classification_aggregate import classification_metrics
from coder_eval.models import (
    ActivationGateVerdict,
    ClassificationCriterionResult,
    EvaluationResult,
    GuardrailCheck,
    NoiseFloor,
    OptimizeMeasurements,
    RegressionRow,
)
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES, cluster_bootstrap_diff_ci, holm_rejections, mean


logger = logging.getLogger(__name__)

# The label whose F1 the activation gate reads. `skill_triggered` emits `yes` / `no`, and
# "did the skill engage when it should" is the `yes` class.
TARGET_LABEL = "yes"

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
            logger.warning("Failed to load %s for the activation gate", task_json, exc_info=True)
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


def _median(values: list[float]) -> float | None:
    """The median, or ``None`` for an empty sample — distinct from a median that happens to be 0.0."""
    return _stats.median(values) if values else None


def cost_latency_guardrails(
    *,
    incumbent_rows: dict[str, list[EvaluationResult]],
    candidate_rows: dict[str, list[EvaluationResult]],
    row_ids: Sequence[str] | None = None,
    materiality: float = MATERIALITY_FLOOR,
    seed: int = 0,
    confidence: float = 0.95,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
) -> list[GuardrailCheck]:
    """Cost and latency guardrails, derived from the measured spread rather than a fixed percentage.

    A fixed tolerance would veto real wins on noise: the measured per-row coefficient of variation is
    ≈0.25, so the standard error of a median over 12 rows is ≈0.09 and a 15% rule sits about 1.5
    standard errors out. So each guardrail runs the SAME paired cluster bootstrap the F1 gate uses,
    over per-row cost / duration, and fails only when even the optimistic end of the interval
    (``ci_low``) is still a material increase — ``ci_low > materiality * incumbent median``.

    ``row_ids`` restricts the comparison to a given row set; the gate passes the rows its F1
    comparison actually used, so the guardrail cannot be computed over a different sample than the
    number it is guarding.

    A measurement that does not exist (no turn reported a cost) passes with a ``note`` and ``None``
    values — never a bare pass, which would read as a pass on the merits.
    """
    ids = sorted(set(incumbent_rows) & set(candidate_rows)) if row_ids is None else list(row_ids)
    checks: list[GuardrailCheck] = []

    for name, extract in (("cost (USD/row)", _row_costs), ("latency (seconds/row)", _row_durations)):
        incumbent_clusters = [extract(incumbent_rows[rid]) for rid in ids]
        candidate_clusters = [extract(candidate_rows[rid]) for rid in ids]
        incumbent_median = _median([mean(c) for c in incumbent_clusters if c])
        candidate_median = _median([mean(c) for c in candidate_clusters if c])

        measured = sum(1 for c in incumbent_clusters if c), sum(1 for c in candidate_clusters if c)
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
        elif incumbent_median == 0.0:
            notes.append("incumbent median is zero, so a relative change is undefined")
            passed, (_diff, ci_low, ci_high, _p) = True, bootstrap
        else:
            _diff, ci_low, ci_high, _p = bootstrap
            passed = ci_low <= materiality * incumbent_median
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


def noise_floor_mde(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    criterion_index: int,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
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
        model=model or _UNRESOLVED_MODEL,
    )
    return measured.mde if measured is not None else None


# Placeholder used when the caller did not resolve a model. It can never collide with a real model
# id, and a floor carrying it is deliberately never cached — see measure_noise_floor.
_UNRESOLVED_MODEL = "(unresolved)"


def measure_noise_floor(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    criterion_index: int,
    model: str,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
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
        return None

    per_dir = [load_suite_rows(d, variant_id, suite_id) for d in run_dirs]
    midpoint = (len(per_dir) + 1) // 2
    first, second = _pool(per_dir[:midpoint]), _pool(per_dir[midpoint:])

    shared = sorted(set(first) & set(second))
    clusters_a = [_label_pairs(first[rid], criterion_index) for rid in shared]
    clusters_b = [_label_pairs(second[rid], criterion_index) for rid in shared]
    scored = [(a, b) for a, b in zip(clusters_a, clusters_b, strict=True) if a and b]
    if len(scored) < 2:
        return None

    probe = NoiseFloor(
        suite_id=suite_id,
        variant_id=variant_id,
        model=model,
        criterion_index=criterion_index,
        n_rows=len(scored),
        n_invocations=len(run_dirs),
        confidence=confidence,
        mde=0.0,
        computed_at=datetime.now(UTC),
    )
    # An unresolved model must never hit the cache: borrowing a floor measured on another model
    # would be worse than the bootstrap it saves.
    if measurements is not None and model != _UNRESOLVED_MODEL:
        cached = lookup_noise_floor(measurements, probe)
        if cached is not None:
            return cached

    bootstrap = cluster_bootstrap_diff_ci(
        [a for a, _b in scored],
        [b for _a, b in scored],
        _f1_yes,
        n_resamples=n_resamples,
        confidence=confidence,
        seed=seed,
    )
    if bootstrap is None:
        return None
    _diff, ci_low, ci_high, _p = bootstrap
    return probe.model_copy(update={"mde": (ci_high - ci_low) / 2.0})


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
        incumbent_pairs = [p for rid in paired_row_ids for p in _label_pairs(incumbent_rows[rid], index)]
        candidate_pairs = [p for rid in paired_row_ids for p in _label_pairs(candidate_rows[rid], index)]
        incumbent_recall = _metric(incumbent_pairs, metric_name)
        candidate_recall = _metric(candidate_pairs, metric_name)

        note: str | None = None
        one_sided = bool(incumbent_pairs) != bool(candidate_pairs)
        if not incumbent_pairs and not candidate_pairs:
            note = f"criterion {index} produced no classification results on either arm — not evaluated"
        elif one_sided:
            missing = "candidate" if incumbent_pairs else "incumbent"
            note = (
                f"criterion {index} produced results on one arm only (the {missing} arm has none) — that is a "
                + "difference between the snapshots, not a regression, so it is reported rather than gated on"
            )
        elif incumbent_recall == 0.0 and candidate_recall == 0.0:
            note = f"{metric_name} is 0.0 on both arms — nothing to regress"

        checks.append(
            GuardrailCheck(
                name=f"sibling {metric_name} [criterion {index}]",
                incumbent=incumbent_recall if incumbent_pairs else None,
                candidate=candidate_recall if candidate_pairs else None,
                relative_change=(
                    (candidate_recall - incumbent_recall) / incumbent_recall
                    if incumbent_recall and not one_sided
                    else None
                ),
                tolerance=tolerance,
                passed=one_sided or not incumbent_pairs or candidate_recall >= incumbent_recall - tolerance,
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
    sibling_indices: Sequence[int] = (),
    materiality: float = MATERIALITY_FLOOR,
    confidence: float = 0.95,
    seed: int = 0,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
) -> ActivationGateVerdict:
    """Gate ONE candidate against the incumbent with a paired cluster bootstrap over rows.

    Each row is a cluster carrying all of its per-invocation label pairs. One draw samples rows
    with replacement, pools each drawn row's replicates, and recomputes ``f1.yes`` per arm through
    the criterion layer's routine; the CI is over ``candidate - incumbent``.

    ``criterion_index`` is the criterion's **position** in the suite's ``success_criteria`` list
    (0-based, counting from the top of the YAML).

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

    incumbent_clusters = [per_row[rid][0] for rid in scored_row_ids]
    candidate_clusters = [per_row[rid][1] for rid in scored_row_ids]

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

    verdict_kwargs = {
        "incumbent_variant": incumbent_variant,
        "candidate_variant": candidate_variant,
        "suite_id": suite_id,
        "criterion_index": criterion_index,
        "confidence": confidence,
        "n_resamples": n_resamples,
        "rows_paired": len(scored_row_ids),
        "rows_excluded": len(unpaired) + unscored_count,
        "sibling_checks": _sibling_checks(
            incumbent_rows=incumbent_rows,
            candidate_rows=candidate_rows,
            paired_row_ids=scored_row_ids,
            sibling_indices=sibling_indices,
        ),
        # Over the rows the F1 comparison actually used, so a guardrail is never computed on a
        # different sample than the number it guards.
        "guardrails": cost_latency_guardrails(
            incumbent_rows=incumbent_rows,
            candidate_rows=candidate_rows,
            row_ids=scored_row_ids,
            materiality=materiality,
            seed=seed,
            confidence=confidence,
            n_resamples=n_resamples,
        ),
        # The MDE is a NULL comparison, and only the incumbent supplies one — splitting the
        # candidate's invocations would measure a different arm's noise.
        "mde": noise_floor_mde(
            run_dirs=incumbent_run_dirs,
            variant_id=incumbent_variant,
            suite_id=suite_id,
            criterion_index=criterion_index,
            confidence=confidence,
            seed=seed,
            n_resamples=n_resamples,
        ),
    }

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
            **verdict_kwargs,
            incumbent_f1=None,
            candidate_f1=None,
            mean_diff=None,
            ci_low=None,
            ci_high=None,
            p_value=None,
            promoted=False,
            notes=notes,
        )

    mean_diff, ci_low, ci_high, p_value = bootstrap

    mde = verdict_kwargs["mde"]
    if isinstance(mde, float) and abs(mean_diff) < mde:
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
        **verdict_kwargs,
        incumbent_f1=_f1_yes(incumbent_pairs),
        candidate_f1=_f1_yes(candidate_pairs),
        mean_diff=mean_diff,
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        range_non_overlap=range_non_overlap,
        notes=notes,
    )


def holm_promote(verdicts: list[ActivationGateVerdict], alpha: float = 0.05) -> list[ActivationGateVerdict]:
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

        siblings_hold = all(check.passed for check in verdict.sibling_checks)
        favours_candidate = verdict.mean_diff is not None and verdict.mean_diff > 0.0
        # The interval must exclude zero as well as the corrected test rejecting. Holm is the
        # stricter of the two almost always, so this changes nothing on a typical family — but it
        # keeps "promote when the interval excludes zero" literally true, which is how the method
        # file states the rule and how anyone reading the rendered block will check it.
        excludes_zero = verdict.ci_low is not None and verdict.ci_low > 0.0
        promoted = i in rejected_at and favours_candidate and siblings_hold and excludes_zero
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
        notes.append(f"Holm applied across a family of {len(family)} at alpha={alpha}.")
        decided.append(verdict.model_copy(update={"promoted": promoted, "holm_alpha": alpha, "notes": notes}))
    return decided


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
        note = f" — {check.note}" if check.note else ""
        lines.append(f"  - {state} · {check.name}: {detail}{interval}{note}")
    return lines


def render_markdown(verdict: ActivationGateVerdict) -> str:
    """The block the skill prints verbatim, numbers and all.

    A verdict whose ``promoted`` is ``None`` renders as **UNDECIDED**, not as a non-promotion.
    Silently reading ``None`` as "not promoted" would let a forgotten :func:`holm_promote` call
    look like an honest negative result — which is the failure this whole gate exists to prevent.
    """
    failed_guardrails = [check.name for check in verdict.guardrails if not check.passed]
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote has not been applied, so this verdict decides nothing"
    elif verdict.promoted and failed_guardrails:
        # `promoted` is the PRIMARY statistic's decision; the guardrails gate in the procedure. A
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
        f"- Rows paired: {verdict.rows_paired} · excluded: {verdict.rows_excluded}",
        f"- f1.yes: incumbent {_fmt(verdict.incumbent_f1)} -> candidate {_fmt(verdict.candidate_f1)}",
        (
            f"- Paired cluster bootstrap (candidate - incumbent): {_fmt(verdict.mean_diff)} "
            + f"{verdict.confidence:.0%} CI [{_fmt(verdict.ci_low)}, {_fmt(verdict.ci_high)}], "
            + f"p = {_fmt(verdict.p_value, '.4f')} over {verdict.n_resamples} draws "
            + f"(p floors at {1.0 / verdict.n_resamples:.4f})"
        ),
        f"- Holm alpha: {_fmt(verdict.holm_alpha, '.3f')}",
        f"- Range non-overlap (DIAGNOSTIC, not the gate): {verdict.range_non_overlap}",
        f"- Minimum detectable effect: {_fmt(verdict.mde)}",
    ]
    lines += _render_checks("Sibling checks", verdict.sibling_checks)
    lines += _render_checks("Guardrails", verdict.guardrails)
    if verdict.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {note}" for note in verdict.notes]
    return "\n".join(lines)
