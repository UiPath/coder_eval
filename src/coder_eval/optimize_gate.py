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
from collections.abc import Sequence
from pathlib import Path

from coder_eval.criteria._classification_aggregate import classification_metrics
from coder_eval.models import ActivationGateVerdict, ClassificationCriterionResult, EvaluationResult, GuardrailCheck
from coder_eval.reports_stats import BOOTSTRAP_RESAMPLES, cluster_bootstrap_diff_ci, holm_rejections


logger = logging.getLogger(__name__)

# The label whose F1 the activation gate reads. `skill_triggered` emits `yes` / `no`, and
# "did the skill engage when it should" is the `yes` class.
TARGET_LABEL = "yes"


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
        promoted = i in rejected_at and favours_candidate and siblings_hold
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
        note = f" — {check.note}" if check.note else ""
        lines.append(f"  - {state} · {check.name}: {detail}{note}")
    return lines


def render_markdown(verdict: ActivationGateVerdict) -> str:
    """The block the skill prints verbatim, numbers and all.

    A verdict whose ``promoted`` is ``None`` renders as **UNDECIDED**, not as a non-promotion.
    Silently reading ``None`` as "not promoted" would let a forgotten :func:`holm_promote` call
    look like an honest negative result — which is the failure this whole gate exists to prevent.
    """
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote has not been applied, so this verdict decides nothing"
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
