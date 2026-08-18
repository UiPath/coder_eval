"""The optimize gate's PRESENTATION half — every markdown block the skill prints verbatim.

Joins the ``reports.py`` / ``reports_experiment.py`` / ``reports_junit.py`` family, and is split
from the optimize-gate family on the precedent :mod:`coder_eval.leak_detection` already set:
the gate decides, this renders the decision, and a decision layer that also owns its presentation
cannot be reviewed as one.

**The layering rule, which a test pins rather than leaving to this sentence** — no filesystem, no
estimator, and **no runtime import of** ``optimize_gate``. A renderer that reaches back for a run
directory or recomputes a statistic is a gate with a table on it, and the module boundary would
then document a separation that does not exist.

The NamedTuples it renders (:class:`~coder_eval.optimize_search.SearchComparison`,
:class:`~coder_eval.optimize_fronts.CostQualityPoint` and
:class:`~coder_eval.optimize_fronts.RuleCeiling`) stay with the code that PRODUCES them and are
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

from coder_eval.models import (
    ActivationGateVerdict,
    ArmRowScores,
    ConfirmVerdict,
    ExecutionGateVerdict,
    GuardrailCheck,
)
from coder_eval.reports_stats import bootstrap_p_floor


if TYPE_CHECKING:
    # Type-only: the two NamedTuples live with the code that PRODUCES them, exactly as
    # `reports_stats.PairedComparison` does for `reports_experiment`. A runtime import here
    # would make the presentation layer depend on the decision layer and turn the split
    # cosmetic — which is what `test_the_presentation_module_makes_no_decisions` pins.
    #
    # TWO modules now, not one: the decision layer was split by track, and these three are
    # produced at its top rank — the fronts and the search loop.
    from coder_eval.optimize_fronts import CostQualityPoint, RuleCeiling
    from coder_eval.optimize_search import SearchComparison


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
    - **BLOCKED BY A GUARDRAIL** — the statistic separated but a check failed. Below the
      refusal, since reading a guardrail presupposes a statistic that separated. Its condition is
      ``holm_rejected and separated and failed``, the execution rung's exact shape, and all three
      conjuncts are load-bearing for the reasons
      :func:`render_execution_markdown`'s docstring gives — ``promoted`` now folds the guardrail
      in, so keying on it retires this rung outright, and ``separated`` alone fires for a candidate
      Holm never rejected. ``failed`` spans BOTH veto lists — the sibling checks and the
      cost/latency guardrails — because both force ``promoted = False``, and a candidate vetoed by
      either one WON its comparison. Reading ``guardrails`` alone sent a sibling regression to the
      ``NOT PROMOTED`` rung, where it read as an ordinary loss.

      The headline names a failing sibling check even though the note ladder does not add a
      ``FAILED — this forces`` line for one. That asymmetry is deliberate: the sibling rung already
      writes a note saying the candidate "moved the failure rather than fixing it", which is more
      than the generic sentence would say, and printing both would say it twice.
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
    # BOTH veto lists, exactly as the execution twin unions `integrity_checks` with `guardrails`.
    # Reading `guardrails` alone was the last place the two tracks disagreed about what `promoted`
    # means: a candidate that separated, cleared Holm and was vetoed by a failing SIBLING check has
    # `promoted=False` and used to render NOT PROMOTED — indistinguishable from one that simply
    # lost, which is the single confusion this rung exists to prevent. "It won and was vetoed" and
    # "it lost" call for opposite next actions.
    failed_guardrails = [check.name for check in (*verdict.sibling_checks, *verdict.guardrails) if not check.passed]
    if verdict.promoted is None:
        headline = "UNDECIDED — holm_promote has not been applied, so this verdict decides nothing"
    elif verdict.gate_refusal is not None and verdict.p_value is None:
        # No p means no comparison was made — a wiring fault, not a resolution limit.
        headline = f"NOT A RESULT — {verdict.gate_refusal}"
    elif verdict.gate_refusal is not None:
        headline = f"CANNOT SEPARATE AT THIS SIZE — {verdict.gate_refusal}"
    elif verdict.holm_rejected and verdict.separated and failed_guardrails:
        # NEVER `promoted`, and never `separated` alone — the execution rung's exact shape, and
        # both exclusions fix a different bug. `promoted` now folds the guardrail in, so keying on
        # it makes this condition unsatisfiable and drops a blocked winner silently into the
        # `NOT PROMOTED` rung below, the one rung it must never be confused with ("it lost" and
        # "it won and was vetoed" call for opposite next actions). `separated` alone fires for a
        # candidate Holm never rejected — measured on the other track: two candidates at p = 0.03
        # in a family of two, identical in every statistic, rendered BLOCKED and NOT PROMOTED
        # purely because one carried a failing cost check, sending that reader to fix cost when
        # the real problem was power.
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
      the refusal, since reading a guardrail presupposes a statistic that separated. Its condition
      is ``holm_rejected and separated and failed``, and all three conjuncts are load-bearing:

      * NEVER ``promoted`` — the guardrail is folded into ``promoted`` by
        ``holm_promote_execution``, so a blocked candidate arrives here with ``promoted is False``
        and reading that field would drop it silently into the ``NOT PROMOTED`` rung below, the one
        rung it must never be confused with ("it lost" and "it won and was vetoed" call for
        opposite next actions).
      * ``separated`` alone is not enough either, and that is the trap on the other side.
        ``separated`` is a property of the verdict and deliberately excludes the FAMILY decision,
        so at ``m > 1`` a p between ``alpha/m`` and ``alpha`` leaves ``ci_low > 0`` while Holm
        rejects nothing. Measured: two candidates at p = 0.03 in a family of two, identical in
        every statistic, rendered ``BLOCKED BY A GUARDRAIL`` and ``NOT PROMOTED`` purely because
        one carried a failing cost check — sending that reader to fix cost when the real problem
        is power, with the note ladder printing the contradicting "did not clear the Holm
        threshold" line directly underneath. ``holm_rejected`` is the field that closes it.

      Both fields live on ``ExecutionGateVerdict`` rather than as helpers here, precisely so this
      file needs no runtime import of ``optimize_gate``.
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
    elif verdict.holm_rejected and verdict.separated and failed:
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
        # A READING, and the line says so: it converts the blended difference above back into the
        # grader's own unit and gates nothing. UNKNOWN rather than 0.000 when it could not be
        # computed — "no dilution" and "we cannot tell" are the two states it exists to separate.
        # The UNKNOWN text names NO cause: there are four (unrecorded weights, fewer than two rows
        # paired, arms whose criteria lists disagree, zero total weight) and only the note knows
        # which. An earlier draft hardcoded "run predates the field", which is a confident wrong
        # sentence on three of the four.
        (
            "- Dead weight: "
            + (
                "UNKNOWN — see notes for why it could not be computed"
                if verdict.dead_weight is None
                else f"{verdict.dead_weight:.1%} of the compared weight (see notes)"
            )
        ),
    ]
    # APPENDED conditionally rather than carried as a `—` on every block: this reading exists only
    # when a primary was predeclared, and a permanent "primary: —" line invites the reader to look
    # for a number nobody asked for. The skill's Step 10 tells the user to read it beside Dead weight,
    # so it has to be on the block rather than only on the model.
    if verdict.primary_criterion_index is not None:
        lines.append(
            f"- Predeclared primary (criterion {verdict.primary_criterion_index}): "
            + f"{_fmt(verdict.primary_mean_diff)} — the same paired difference on that criterion "
            + "ALONE, in the unit it scores in. A reading: it gates nothing."
        )
    lines += _render_checks("Integrity checks", verdict.integrity_checks)
    lines += _render_checks("Guardrails", verdict.guardrails)
    if verdict.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {note}" for note in verdict.notes]
    return "\n".join(lines)


def render_confirm_markdown(verdict: ConfirmVerdict) -> str:
    """The Stage C block the skill prints verbatim, on either track.

    **Its headlines share NOTHING with the gate renderers, and that is worth stating plainly** because
    an earlier draft of this docstring claimed they did: the four here are its own, only REVERSED
    resembles anything on the gate ladders, and there is no PRECEDENCE to implement at all —
    ``outcome`` is a mutually-exclusive ``Literal``, so this is one lookup plus one override. What IS
    carried over from :func:`render_execution_markdown` is the two RULES that ladder exists to
    enforce, and they are the whole reason this function is not a dict:

    - **A refusal takes the headline and outranks every outcome.** ``confirm_refusal`` means the block
      is not a comparison, so no classification beneath it means anything. It is printed ONCE, in the
      headline and not also as a note — the rule ``holm_promote`` states for ``gate_refusal``, which
      :func:`build_confirm_verdict` implements by leaving the note off a refused block.
    - **REVERSED is a headline, not a footnote.** It says the effect the round was built on pointed
      the other way on held-out rows, and a reader who skims past it promotes on a number that does
      not hold. It reads "Do not promote" for that reason.

    The full confirm-gate block is NOT re-rendered here: this block reports the comparison and names
    which renderer to print beside it, because the two verdict types have their own ladders and a
    third copy of either is the drift a hand-written ladder produces.
    """
    headline = {
        "reversed": "REVERSED — the effect points the other way on held-out rows. Do not promote.",
        "shrank": "SHRANK — the effect is real but smaller than the train block claimed.",
        "reproduced": "REPRODUCED — the effect holds on the held-out split.",
        "undecided": "UNDECIDED — this is not a train-to-test comparison.",
    }[verdict.outcome]
    if verdict.confirm_refusal is not None:
        headline = f"NOT A COMPARISON — {verdict.confirm_refusal}"

    lines = [
        f"### Stage C confirm — `{verdict.candidate_variant}` vs `{verdict.incumbent_variant}`",
        "",
        f"**{headline}**",
        "",
        f"- Suite `{verdict.suite_id}`, family of ONE (only the Stage B winner is confirmed)",
        f"- Train effect (candidate - incumbent): {_fmt(verdict.train_effect)}",
        f"- Test effect on the held-out split: {_fmt(verdict.test_effect)}",
        f"- Delta: {_fmt(verdict.delta)} · confirm split's own MDE: {_fmt(verdict.test_mde)}",
        f"- Outcome: **{verdict.outcome.upper()}**",
    ]
    if verdict.notes:
        lines.append("- **Notes:**")
        lines += [f"  - {note}" for note in verdict.notes]
    lines += [
        "",
        "The confirm gate's own block is carried on `test_verdict` — print it with "
        + "`render_markdown` (activation) or `render_execution_markdown` (execution) beside this one.",
    ]
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


# What a single-replicate matrix is and is not, in one place. Rendered by `render_row_matrix` and
# asserted against the skill's prose by a sensor that IMPORTS it, so the claim cannot exist in two
# files at two vintages — the shape `COST_FRONT_ADVISORY` already has.
SINGLE_REPLICATE_CAVEAT = (
    "ONE replicate per row: this matrix RANKS, it does not MEASURE. Every cell is a single draw, "
    "so a difference here is a hypothesis for the gate rather than an effect. Measured on one "
    "round: a single-replicate matrix reported +0.0392 against a 0.0255 floor and put the "
    "incumbent off the Pareto front; the replicated gate over the same rows returned 0.000, "
    "p = 0.9977."
)


def render_row_matrix(
    arms: list[ArmRowScores],
    pareto: list[str],
    *,
    instance_best: list[str] | None = None,
    n_replicates: int | None = None,
) -> str:
    """The row x candidate table, with the Pareto set marked and the holes made visible.

    ``instance_best`` is keyword-only and optional so the existing two-positional-argument form
    keeps working byte-for-byte. When given, the block names both fronts AND the arms they disagree
    about — a reader shown two lists learns nothing; the diff is the finding.

    ``n_replicates`` is added the same way and for the same reason: omitted, the output is
    byte-identical to what every existing call site already prints. Given, the block says how many
    draws each cell averages — and at **one** it prints
    :data:`SINGLE_REPLICATE_CAVEAT`, because a Stage A matrix is a ranking device and reads exactly
    like a measurement.
    """
    if not arms:
        return "_No arms to compare._"

    row_ids = sorted({rid for arm in arms for rid in arm.row_scores})
    replicate_note: list[str] = []
    if n_replicates is not None:
        replicate_note = [f"Each cell is the mean of {n_replicates} replicate(s)."]
        if n_replicates <= 1:
            replicate_note.append(SINGLE_REPLICATE_CAVEAT)
        replicate_note.append("")
    return "\n".join(
        [
            *replicate_note,
            *_matrix_table(arms, row_ids, pareto),
            "",
            *_front_summary(pareto, instance_best),
            *_matrix_footnotes(arms, row_ids),
        ]
    )


def _spread(values: list[float]) -> str:
    """One arm's replicate values on one row, and their spread.

    A SINGLE replicate's spread is undefined, not zero, and renders as ``—``. Printing 0.0 would
    fire the zero-variance flag on every row of a single-replicate run, where it means nothing —
    the flag's whole value is that it identifies a REPRODUCIBLE difference.
    """
    listed = ", ".join(f"{v:.3f}" for v in values) or "—"
    return f"{listed} (spread {max(values) - min(values):.3f})" if len(values) > 1 else listed


def render_row_replicates(incumbent: dict[str, list[float]], candidate: dict[str, list[float]]) -> str:
    """Per-row replicate values for two arms, with the zero-variance and dead rows named.

    Takes **already-extracted** replicate values rather than results: pulling a score out of an
    ``EvaluationResult`` needs the loader's private row primitive, which this module may not import
    at runtime, and the criterion to read it from is a choice every other execution-track function
    already leaves with the caller. So the caller extracts and this stays pure formatting.

    Two things a suite mean cannot say, and the reason this block exists:

    * **A row with zero variance on BOTH arms is the most informative row in the run.** It is a
      reproducible behavioural change, which is exactly the raw material a merge candidate is built
      from. Measured: one row went ``[0.76, 0.76, 0.76]`` -> ``[1.00, 1.00, 1.00]`` (+0.240) while
      another went ``[0.86, 0.86, 0.86]`` -> ``[0.59, 0.59, 0.59]`` (-0.270). They cancelled to a
      suite delta of +0.0001 — "the difference is noise" is the opposite of what happened, and no
      verdict block could express it.
    * **A row whose mean delta is exactly 0.0 is DEAD for this comparison** and is counted. A suite
      of dead rows is a suite that cannot resolve anything, however many rows it has.

    A row present on one arm only is a HOLE: shown, excluded from the delta, never counted as 0.0 —
    the convention ``ArmRowScores`` and :func:`render_row_matrix` already use. Unequal replicate
    counts between the arms are REPORTED rather than silently paired, because a row weighted 3-v-2
    has reweighted the comparison on its own.
    """
    row_ids = sorted(set(incumbent) | set(candidate))
    if not row_ids:
        return "_No rows to compare._"

    lines = ["| row | incumbent | candidate | mean delta |", "|---|---|---|---|"]
    dead: list[str] = []
    reproducible: list[str] = []
    unequal: list[str] = []
    for row_id in row_ids:
        left, right = incumbent.get(row_id, []), candidate.get(row_id, [])
        if not left or not right:
            lines.append(f"| {row_id} | {_spread(left)} | {_spread(right)} | — (hole) |")
            continue
        delta = sum(right) / len(right) - sum(left) / len(left)
        lines.append(f"| {row_id} | {_spread(left)} | {_spread(right)} | {delta:+.3f} |")
        if delta == 0.0:
            dead.append(row_id)
        if len(left) > 1 and len(right) > 1 and max(left) == min(left) and max(right) == min(right) and delta != 0.0:
            reproducible.append(row_id)
        if len(left) != len(right):
            unequal.append(f"{row_id} ({len(left)} v {len(right)})")

    lines.append("")
    if reproducible:
        lines.append(
            f"Zero variance on BOTH arms, and a non-zero delta: {', '.join(reproducible)}. These are "
            + "REPRODUCIBLE behavioural changes rather than noise — the most informative rows in the "
            + "run, and what a merge candidate should be built from. Read them individually; two of "
            + "them with opposite signs cancel to a suite delta of nearly zero."
        )
    if dead:
        lines.append(
            f"{len(dead)} row(s) dead for this comparison (mean delta exactly 0.0): {', '.join(dead)}. "
            + "They contribute nothing to the paired difference — a suite that is mostly dead rows "
            + "cannot resolve anything, however many rows it has."
        )
    if unequal:
        lines.append(
            f"Rows whose arms carry different replicate counts: {', '.join(unequal)}. A row's weight "
            + "in an arm's mean is its observation count, so this shifts the comparison on its own — "
            + "usually an interrupted invocation. Re-run it rather than reading the delta as an effect."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The headroom ceiling — what the suite can resolve, before a candidate is written
# ---------------------------------------------------------------------------


# The margin a rule's ceiling should carry over the noise floor before a candidate for it is worth
# writing. A candidate cannot be expected to capture ALL of a rule's headroom, so a ceiling merely
# at the floor demands a perfect candidate to register at all. Three is a convention, not an
# estimate, and the render says so rather than dressing it as one.
CEILING_MARGIN = 3.0


def _ceiling_verdict(entry: RuleCeiling, floor: float) -> str:
    """One rule's read on its own ceiling, given the suite's measured noise floor.

    Takes the whole record rather than the ceiling alone, because the two states that must NOT
    read as a gap are visible only in the counts. A gap says "stop working on this rule", which is
    the most expensive advice here, and it must never be produced by an absence of data:

    * **Every selected row missing from the vector** is a wiring fault — a stale rule map, a
      crashed arm — not a rule without headroom, and the whole family goes out of its way to keep
      those apart (``_wrong_path_reason``, ``_no_floor``).
    * **A floor of exactly 0.0 is a real answer**, not a missing one: a deterministic grader whose
      replicates agree measures no noise. But ``ceiling < 0.0`` is then false for a rule with NO
      headroom at all, so the one rule nothing can improve would collect the most encouraging
      verdict. A zero ceiling is a gap whatever the floor.
    """
    if entry.n_failing == 0 and entry.n_dropped:
        return "NO DATA — every row attributed to this rule is missing from the vector; a wiring fault, not a gap"
    if entry.ceiling <= 0.0:
        # Covers both shapes: no row was attributed to the rule at all (it always passed, so
        # `rule_row_map` gives it an empty set), and every row that was is already at the maximum.
        # Neither leaves a candidate anywhere to gain, which is the only thing the verdict claims.
        return "GAP — no headroom: this suite has no row where a candidate for this rule could gain"
    if entry.ceiling < floor:
        return "GAP — no candidate for this rule can promote; the remedy is ROWS, not candidates"
    if entry.ceiling < CEILING_MARGIN * floor:
        return f"thin — a candidate would have to capture nearly all of it (under {CEILING_MARGIN:g}x the floor)"
    return "room for a candidate"


def render_headroom_ceilings(ceilings: list[RuleCeiling], floor: float | None, *, unattributed: int = 0) -> str:
    """What each rule could move the suite mean by at MOST, against the floor that must be cleared.

    Read before Step 8 proposes anything: a rule whose ceiling is below the floor is a **suite
    gap, not a hypothesis**, and no wording of a candidate can fix it. Measured on a real round,
    three of four rules were unpromotable by arithmetic and roughly $40 was spent gating candidates
    for them — off inputs (a baseline, a noise floor) already paid for.

    ``floor is None`` — no noise floor could be measured — renders the ceilings **without**
    verdicts rather than inventing a threshold. A ceiling with no floor still says which rule has
    the most room; a fabricated floor says nothing true at all.

    ``unattributed`` is the count of rows that carried no ``RULES`` line
    (:attr:`~coder_eval.optimize_load.RuleAttribution.unattributed`), and it is keyword-only and
    defaulted so existing calls are unchanged. When non-zero the block says every ceiling below is
    an **under**-estimate — a row nothing attributed is in no rule's failing set, so its headroom
    is counted in no rule. Without that line a ``GAP`` verdict, which tells a reader to stop
    working on a rule, can be produced by a stdout the criterion truncated at 4000 characters.

    Advisory, always. See :func:`~coder_eval.optimize_fronts.headroom_ceiling` for why a table
    built on an AUTHORED attribution must never be able to block a promotion.
    """
    if not ceilings:
        return "_No rules to size._"

    header = "| rule | rows failing | headroom | ceiling |" + (" x floor | verdict |" if floor is not None else "")
    lines = [header, "|" + "---|" * (6 if floor is not None else 4)]
    for entry in ceilings:
        # The suite-level entry carries no rule; naming it is what keeps it from reading as a rule
        # called "" whose ceiling happens to be every row's headroom.
        name = f"`{entry.rule}`" if entry.rule else "**whole suite**"
        row = f"| {name} | {entry.n_failing} | {entry.headroom:.3f} | {entry.ceiling:.4f} |"
        if floor is not None:
            # `—` rather than `inf` at a zero floor: a deterministic grader legitimately measures
            # no noise, and "infinitely above the floor" is not a reading anyone can act on.
            ratio = f"{entry.ceiling / floor:.2f}x" if floor > 0.0 else "—"
            row += f" {ratio} | {_ceiling_verdict(entry, floor)} |"
        lines.append(row)

    lines.append("")
    if floor is None:
        lines.append(
            "No noise floor was measured, so no verdict is rendered. A ceiling still ranks the "
            + "rules by how much room they have; whether that room clears the noise is the one "
            + "thing this block cannot say."
        )
    else:
        lines.append(
            f"Floor {floor:.4f}. A ceiling BELOW it is a suite gap: no candidate for that rule can "
            + "promote, however good, because the suite mean cannot move that far. The remedy is "
            + f"rows that fail the rule — about `{CEILING_MARGIN:g} x floor x n_rows` of headroom "
            + "for a comfortable margin. Every row that PASSES a rule makes that rule harder to "
            + "promote, which is why one row per rule is the worst possible suite shape."
        )

    if unattributed:
        lines.append(
            f"**{unattributed} row(s) carried no rule attribution and are counted in NO rule's "
            + "headroom, so every ceiling above is an UNDER-estimate.** A GAP verdict here may be "
            + "a truncated grader log rather than a rule without room — `run_command` caps each "
            + "stream at 4000 characters. Fix the attribution before acting on a gap."
        )

    dropped = [f"{c.rule or 'whole suite'} ({c.n_dropped})" for c in ceilings if c.n_dropped]
    if dropped:
        lines.append(
            f"Rule rows that could not be scored and were left out of the headroom: {', '.join(dropped)}. "
            + "A stale rule map naming rows this run did not produce would otherwise inflate a ceiling "
            + "silently — these are excluded, never counted at zero."
        )
    lines.append(
        "Advisory. Rule attribution is authored, so a mistyped rule id moves rows between rules — "
        + "this block tells you what to stop paying for, and never blocks a promotion."
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
