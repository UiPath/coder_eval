"""The optimize gate's PRESENTATION half — every markdown block the skill prints verbatim.

Joins the ``reports.py`` / ``reports_experiment.py`` / ``reports_junit.py`` family, and is split
from :mod:`coder_eval.optimize_gate` on the precedent :mod:`coder_eval.leak_detection` already set:
the gate decides, this renders the decision, and a decision layer that also owns its presentation
cannot be reviewed as one.

**The layering rule, which a test pins rather than leaving to this sentence** — no filesystem, no
estimator, and **no runtime import of** ``optimize_gate``. A renderer that reaches back for a run
directory or recomputes a statistic is a gate with a table on it, and the module boundary would
then document a separation that does not exist.

The two NamedTuples it renders (:class:`~coder_eval.optimize_gate.SearchComparison`,
:class:`~coder_eval.optimize_gate.CostQualityPoint`) stay with the code that PRODUCES them and are
imported here under ``if TYPE_CHECKING`` only — exactly the shape
``reports_stats.PairedComparison`` → ``reports_experiment`` already has.

The one estimator value it may read is :func:`coder_eval.reports_stats.bootstrap_p_floor`, for
display in ``render_markdown``'s "p floors" line. CE040 requires it be DERIVED there rather than
respelled, so the import is the rule being followed, not an exception to it.

**A library, not a CLI**, like its two siblings: no typer, no rich, no reach into the CLI package.
The skill drives these functions from a short inline ``python`` snippet and prints the returned
``str``. (Spelled that way on purpose — the assertion enforcing it is a raw-substring scan, so
naming the banned module here would trip a sensor on its own documentation.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coder_eval.models import ActivationGateVerdict, ArmRowScores, ExecutionGateVerdict, GuardrailCheck
from coder_eval.reports_stats import bootstrap_p_floor


if TYPE_CHECKING:
    # Type-only: the two NamedTuples live with the code that PRODUCES them, exactly as
    # `reports_stats.PairedComparison` does for `reports_experiment`. A runtime import here
    # would make the presentation layer depend on the decision layer and turn the split
    # cosmetic — which is what `test_the_presentation_module_makes_no_decisions` pins.
    from coder_eval.optimize_gate import CostQualityPoint, SearchComparison


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

    Five headlines, in this precedence, and each is a different claim:

    - **UNDECIDED** — ``promoted`` is ``None``, so :func:`holm_promote` never ran. It outranks
      everything below because a verdict Holm never saw has no threshold to be refused against.
      Silently reading ``None`` as "not promoted" would let a forgotten call look like an honest
      negative result — the failure this whole gate exists to prevent.
    - **NOT A RESULT** — ``gate_refusal`` is set AND ``p_value is None``: there was no comparison
      to make. On this track that is the cross-split preflight — the two arms scored different row
      sets, so their difference is not an effect. Deliberately the vocabulary the execution
      renderer already uses, because it is the same claim.
    - **CANNOT SEPARATE AT THIS SIZE** — ``gate_refusal`` is set and a p WAS computed: the suite's
      discreteness floor exceeds the Holm threshold, so no candidate could promote however good it
      is. It outranks NOT PROMOTED because it is a statement about the suite, not this candidate.
    - **BLOCKED BY A GUARDRAIL** — the statistic separated but a guardrail failed. Below the
      refusal, since reading a guardrail presupposes a statistic that separated.
    - **PROMOTED / NOT PROMOTED** — the ordinary outcomes.

    **What separates the two refusals is the p, not a second field.** A discreteness refusal is a
    statement about the suite's RESOLUTION and is only ever set inside ``holm_promote``'s
    ``p_value is not None`` branch, so it always carries one. A wiring refusal says NO COMPARISON
    WAS MADE and never does. One boolean on a value the model already carries, rather than a field
    two setters would have to agree about.

    ``UNDECIDED`` outranking both refusals is right — a verdict Holm never saw has no decision to
    refuse — but the refusal's TEXT must still reach the reader, so it is printed on its own line
    whenever the headline could not carry it. Without that, a pre-Holm cross-split block renders a
    confident ``UNDECIDED`` with the reason nowhere on the page. The execution renderer solved
    exactly this and its comment records why.
    """
    failed_guardrails = [check.name for check in verdict.guardrails if not check.passed]
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote has not been applied, so this verdict decides nothing"
    elif verdict.gate_refusal is not None and verdict.p_value is None:
        # No p means no comparison was made — a wiring fault, not a resolution limit.
        headline = f"NOT A RESULT — {verdict.gate_refusal}"
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
    ]
    # Exactly the one path where the headline did not carry it — so the message appears once,
    # never twice. Mirrors the execution renderer, for the reason its own comment gives.
    if verdict.gate_refusal is not None and verdict.promoted is None:
        lines += [f"**NOT A RESULT:** {verdict.gate_refusal}", ""]
    lines += [
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


def render_execution_markdown(verdict: ExecutionGateVerdict) -> str:
    """The block the skill prints verbatim, mirroring :func:`render_markdown`'s headline precedence.

    Four headlines, in this precedence:

    - **UNDECIDED** — Holm has not run, so there is no decision to refuse or report.
    - **NOT A RESULT** — ``gate_refusal`` is set: there was no comparison to make, an arm loaded
      zero rows, fewer than two rows paired, the paired differences carry zero variance, or the
      difference is below the suite's own MDE with an interval that still excludes zero. It
      outranks everything below because it says the sample decided nothing, and the
      message names which cause and its remedy. Deliberately NOT the
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


def _matrix_table(arms: list[ArmRowScores], row_ids: list[str], pareto: list[str]) -> list[str]:
    """The header, the separator and one line per row. A hole renders as ``—``, never as 0.0."""
    header = " | ".join(f"**{a.variant_id}**" if a.variant_id in pareto else a.variant_id for a in arms)
    lines = [
        f"| row | {header} |",
        "|" + "---|" * (len(arms) + 1),
    ]
    for row_id in row_ids:
        cells = " | ".join(f"{a.row_scores[row_id]:.3f}" if row_id in a.row_scores else "—" for a in arms)
        lines.append(f"| {row_id} | {cells} |")
    return lines


def _front_summary(pareto: list[str], instance_best: list[str] | None) -> list[str]:
    """The front block: the Pareto line always, and the instance-best pair only when asked.

    ``None`` and ``[]`` are DIFFERENT and the distinction is the legacy call shape. ``None`` is the
    two-positional-argument form, which must emit neither the instance-best line nor an agreement
    sentence; ``[]`` is a real, empty instance-best front and emits ``… : none``.
    """
    lines = [f"Pareto front (**bold**): {', '.join(pareto) if pareto else 'none'}"]
    if instance_best is None:
        return lines

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
    return lines


def _matrix_footnotes(arms: list[ArmRowScores], row_ids: list[str]) -> list[str]:
    """The three things the table alone cannot say: unscored arms, holes, and all-zero rows."""
    lines: list[str] = []
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
    return lines


def render_row_matrix(arms: list[ArmRowScores], pareto: list[str], *, instance_best: list[str] | None = None) -> str:
    """The row x candidate table, with the Pareto set marked and the holes made visible.

    ``instance_best`` is keyword-only and optional so the existing two-positional-argument form
    keeps working byte-for-byte. When given, the block names both fronts AND the arms they disagree
    about — a reader shown two lists learns nothing; the diff is the finding.
    """
    if not arms:
        return "_No arms to compare._"

    row_ids = sorted({rid for arm in arms for rid in arm.row_scores})
    return "\n".join(
        [
            *_matrix_table(arms, row_ids, pareto),
            "",
            *_front_summary(pareto, instance_best),
            *_matrix_footnotes(arms, row_ids),
        ]
    )


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
