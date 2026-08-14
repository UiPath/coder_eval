"""Shared classification aggregator used by label-emitting criteria.

Consumers (``classification_match``, ``skill_triggered``) return
``ClassificationCriterionResult`` per row; this module consumes the
per-row pairs and overlays sklearn-style classification metrics on top
of the baseline stats already computed by ``BaseCriterion.aggregate``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from coder_eval.models import (
    ClassificationCriterionResult,
    ClassLabelStats,
    ConfusionEntry,
    CriterionAggregate,
    CriterionResult,
)


class ClassificationMetrics(NamedTuple):
    """Everything :func:`classification_metrics` derives from a list of label pairs."""

    metrics: dict[str, float]
    per_label: list[ClassLabelStats]
    confusion: list[ConfusionEntry]
    labels: list[str]

    def metric(self, name: str) -> float:
        """A metric by name, applying this module's div-by-zero convention to an ABSENT key.

        A label that never appears in either position produces no ``f1.<label>`` entry at all,
        which is the same *situation* as a zero denominator and must read the same way: 0.0.
        Consumers must call this rather than ``.metrics[name]`` or ``.metrics.get(name, 0.0)``:
        the 0.0 convention is owned by this module, and a default spelled at the call site is a
        second declaration of it that can drift. This particular rule is a convention, enforced
        by review — CE037 guards the *arithmetic* next door, not this accessor.
        """
        return self.metrics.get(name, 0.0)


def classification_metrics(pairs: list[tuple[str, str]]) -> ClassificationMetrics | None:
    """Accuracy / per-label precision, recall, F1 / micro + macro + weighted F1 / confusion.

    **The single implementation of that arithmetic in this package** — CE037 fails the build on
    a second one anywhere under ``src/coder_eval/``. A consumer that re-derives F1 from the
    confusion counts inherits a div-by-zero convention that can drift from this one, and would
    then disagree with the gate the run itself applied.

    Pure ``pairs -> metrics``: it takes no base aggregate, so callers outside the criterion layer
    (the optimize gate's bootstrap resamples label pairs directly) reach the same numbers.
    Returns ``None`` for empty ``pairs`` — there is nothing to measure, which is distinct from
    measuring zero.

    Div-by-zero convention: precision / recall / F1 are 0.0 when the denominator is 0 (no
    predictions for a class, or no true instances). :meth:`ClassificationMetrics.metric` extends
    the same convention to a metric name that is absent entirely.
    """
    if not pairs:
        return None

    labels = sorted({lbl for pair in pairs for lbl in pair})
    metrics: dict[str, float] = {}
    per_label_stats: list[ClassLabelStats] = []
    micro_tp = micro_fp = micro_fn = 0

    for c in labels:
        tp = sum(1 for e, o in pairs if e == c and o == c)
        fp = sum(1 for e, o in pairs if e != c and o == c)
        fn = sum(1 for e, o in pairs if e == c and o != c)
        support = sum(1 for e, _ in pairs if e == c)

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

        per_label_stats.append(ClassLabelStats(label=c, precision=precision, recall=recall, f1=f1, support=support))
        metrics[f"precision.{c}"] = precision
        metrics[f"recall.{c}"] = recall
        metrics[f"f1.{c}"] = f1

        micro_tp += tp
        micro_fp += fp
        micro_fn += fn

    accuracy = sum(1 for e, o in pairs if e == o) / len(pairs)
    macro_f1 = sum(s.f1 for s in per_label_stats) / len(per_label_stats) if per_label_stats else 0.0
    total_support = sum(s.support for s in per_label_stats)
    weighted_f1 = sum(s.f1 * s.support for s in per_label_stats) / total_support if total_support else 0.0
    micro_precision = micro_tp / (micro_tp + micro_fp) if (micro_tp + micro_fp) else 0.0
    micro_recall = micro_tp / (micro_tp + micro_fn) if (micro_tp + micro_fn) else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if (micro_precision + micro_recall)
        else 0.0
    )

    metrics["accuracy"] = accuracy
    metrics["macro_f1"] = macro_f1
    metrics["weighted_f1"] = weighted_f1
    metrics["micro_f1"] = micro_f1

    conf_counts: dict[tuple[str, str], int] = defaultdict(int)
    for e, o in pairs:
        conf_counts[(e, o)] += 1
    confusion = [ConfusionEntry(expected=e, observed=o, count=cnt) for (e, o), cnt in sorted(conf_counts.items())]

    return ClassificationMetrics(metrics=metrics, per_label=per_label_stats, confusion=confusion, labels=labels)


def overlay_classification_metrics(
    base: CriterionAggregate,
    per_row_results: list[CriterionResult],
) -> CriterionAggregate:
    """Merge accuracy / recall / F1 / confusion into an existing aggregate.

    Reads ``(expected_label, observed_label)`` pairs from rows that produced
    a ``ClassificationCriterionResult`` and layers the following onto the
    base aggregate:

      - ``accuracy``, ``macro_f1``, ``weighted_f1``, ``micro_f1``
      - ``precision.<label>``, ``recall.<label>``, ``f1.<label>`` per label
      - ``details.labels``, ``details.per_label``, ``details.confusion``,
        ``details.total_pairs`` for markdown rendering

    When no row emitted classification labels, returns ``base`` unchanged
    (so users can still threshold on baseline mean / min / etc.).

    Div-by-zero convention: precision / recall / F1 are 0.0 when the
    denominator is 0 (no predictions for a class, or no true instances).

    The arithmetic itself lives in :func:`classification_metrics`; this function
    only pairs it with the base aggregate the criterion layer already computed.
    """
    pairs: list[tuple[str, str]] = [
        (r.expected_label, r.observed_label) for r in per_row_results if isinstance(r, ClassificationCriterionResult)
    ]
    cm = classification_metrics(pairs)
    if cm is None:
        return base

    # Baseline stats first; classification overlays on top.
    metrics = {**base.metrics, **cm.metrics}

    details = dict(base.details)
    details.update(
        {
            "labels": cm.labels,
            "per_label": [s.model_dump() for s in cm.per_label],
            "confusion": [c.model_dump() for c in cm.confusion],
            "total_pairs": len(pairs),
        }
    )

    return base.model_copy(update={"metrics": metrics, "details": details})
