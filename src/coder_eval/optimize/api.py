"""The ONE module `/coder-eval:optimize-skill`'s `SKILL.md` imports — the declared skill-facing API.

Rank 4 of the optimize family: above every decision rank, and the only member that may import the
presentation half. See :mod:`coder_eval.optimize` for the ladder itself; it is not restated here.

**Every function here COMPOSES.** It decides nothing — the gates, floors and fronts below it do
that — and it formats nothing: every block it returns comes from a ``render_*`` function in
:mod:`coder_eval.reports_optimize`, which is where the claim "every markdown block the skill
prints" lives. What a composite owns is the part that used to live in markdown: the guards, the
fallbacks, the track branch, and the order in which the primitives are called.

**Every function returns the markdown block the skill prints**, with its reading stated in words
inside the block. That is why there is no wrapper model: a caller prints the string into its ledger
and a reader of that ledger sees the same sentences the caller acted on.

**A name beginning ``record_`` WRITES** — the ``measurements.json`` sidecar, the regression corpus.
Everything named ``*_report`` reads and returns; nothing else here touches disk for writing.

**Run directories are ``Path``, and that is enforced rather than documented.** A bare string is a
``Sequence`` too, so it iterates into characters and fails somewhere downstream — after the runs it
was meant to read have been paid for. Every entry point rejects one at the boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from coder_eval.models import ACTIVATION_FLOOR_METRIC, EXECUTION_FLOOR_METRIC
from coder_eval.optimize.activation import min_discordant_rows, noise_floor_mde
from coder_eval.optimize.execution import measure_execution_noise_floor, resolve_arm_model
from coder_eval.optimize.fronts import (
    arm_row_scores,
    cost_quality_front,
    cost_quality_points,
    headroom_ceiling,
    instance_best_front,
    pareto_front,
)
from coder_eval.optimize.load import load_arm_rows, reconcile_arms, rule_row_map, stale_tree_reason
from coder_eval.optimize.search import regression_check
from coder_eval.optimize.store import UNRESOLVED_MODEL, load_measurements
from coder_eval.reports_optimize import (
    render_attribution_unavailable,
    render_corpus_check,
    render_cost_quality,
    render_discreteness,
    render_headroom_ceilings,
    render_noise_floor,
    render_row_matrix,
    render_staleness_note,
)
from coder_eval.reports_stats import DEFAULT_ALPHA


def _require_run_dirs(run_dirs: Sequence[Path]) -> None:
    """Reject a non-sequence, a string and an empty sequence, BEFORE any filesystem access.

    Three shape mistakes, and keeping them apart is the point, because two of the three otherwise
    arrive as a MEASUREMENT rather than an error:

    * A ``str`` is a ``Sequence`` that iterates into single characters, so it reaches the estimator
      as one run directory per letter and comes back as "no rows found".
    * An EMPTY sequence loads nothing, so the estimator reports the wrong-path refusal for a call
      that named no path at all.
    * A bare ``Path`` — the likeliest of the three, since every one of these takes a list where the
      underlying gate takes one directory — is not iterable, so without this it raises from
      whichever comprehension reaches it first, naming neither the argument nor the fix.

    All three raise, and none renders, because a rendered block is a reading of a suite — and none
    of these got as far as reading one.
    """
    if isinstance(run_dirs, str | bytes | Path):
        raise TypeError(
            f"run_dirs must be a sequence of pathlib.Path, not {type(run_dirs).__name__} — pass "
            + "[dir], not dir (a string would be read as one run directory per letter)"
        )
    if not run_dirs:
        raise ValueError("run_dirs is empty — no run directory was named, so there is nothing to measure")
    wrong = [d for d in run_dirs if isinstance(d, str | bytes)]
    if wrong:
        raise TypeError(
            f"run_dirs holds {len(wrong)} string(s) rather than pathlib.Path ({wrong[0]!r}) — these are "
            + "joined with `/`, which a string does not support"
        )


def activation_floor_report(
    *,
    run_dirs: Sequence[Path],
    suite_id: str,
    criterion_index: int,
    sidecar: Path,
    variant_id: str = "default",
) -> str:
    """The activation track's noise floor over a baseline arm, priced before anything is proposed.

    The null comparison splits the baseline's own invocations, so ``run_dirs`` is the two (or more)
    ``coder-eval run`` invocations of the SAME arm — fewer than two is a rendered refusal, not an
    error, because it is a fact about the sample.

    The sidecar is read BEFORE the bootstrap, which is the only moment the floor cache can save
    anything: a stored floor for the same key is returned instead of recomputed.
    """
    _require_run_dirs(run_dirs)
    reasons: list[str] = []
    mde = noise_floor_mde(
        run_dirs=run_dirs,
        variant_id=variant_id,
        suite_id=suite_id,
        criterion_index=criterion_index,
        measurements=load_measurements(sidecar),
        # `None` is passed through rather than substituted: the activation floor's `model` parameter
        # is `str | None` and does its own substitution, and inventing one here would key a cache
        # entry on a model nobody resolved.
        model=resolve_arm_model(run_dirs, variant_id, suite_id),
        reasons=reasons,
    )
    return render_noise_floor(
        mde,
        metric=ACTIVATION_FLOOR_METRIC,
        # At most one cause is ever recorded — every refusal in the estimator is a `return` — and
        # naming it is the whole reason the sink is threaded: a bare "no floor" reads as "suite too
        # small" and sends a reader to buy rows a mistyped criterion_index does not need.
        reason=reasons[0] if reasons else None,
    )


def execution_floor_report(*, run_dirs: Sequence[Path], variant_id: str, suite_id: str, sidecar: Path) -> str:
    """The execution track's noise floor over the control arm — a null split over REPLICATES.

    Read after the control arm and before Stage A: it cannot save the control spend, but Stage A,
    B and C are the stages that multiply by candidate count and they are all still unspent.

    An unresolvable model is recorded as ``UNRESOLVED_MODEL`` rather than passed as ``None``, which
    the record's ``model`` field forbids. That is deliberately NOT what the activation twin does:
    there the parameter is optional and substitutes for itself.
    """
    _require_run_dirs(run_dirs)
    floor = measure_execution_noise_floor(
        run_dirs=run_dirs,
        variant_id=variant_id,
        suite_id=suite_id,
        model=resolve_arm_model(run_dirs, variant_id, suite_id) or UNRESOLVED_MODEL,
        measurements=load_measurements(sidecar),
    )
    return render_noise_floor(
        None if floor is None else floor.mde,
        metric=EXECUTION_FLOOR_METRIC,
        # This estimator threads no `reasons` sink — it logs its refusal instead — so on this track
        # the block cannot name which of its FOUR preconditions failed (a stale tree, nothing
        # loaded, a split mismatch, or too few replicated rows). Stated rather than discovered: the
        # gap is an open item in `.claude/harness-candidates.md`, and closing it belongs to the
        # estimator rather than here — a composite that guessed at the cause would be worse than one
        # that says none was recorded.
        reason=None,
        # The sample shape, which only this track has: `n_replicates` is what tells a reader whether
        # they are holding a two-replicate floor or a three-replicate one, and the skill's own prose
        # calls that the expensive distinction.
        n_rows=None if floor is None else floor.n_rows,
        n_replicates=None if floor is None else floor.n_replicates,
    )


def discreteness_report(*, rows: int, survivors: int) -> str:
    """How many rows the arms must disagree on before any candidate can promote — activation track.

    The second thing a suite fails on, and the cheaper of the two to read: pure arithmetic over the
    row count and the family size, so it costs nothing and can stop a whole round.

    ``survivors`` is how many candidates Stage B will gate, because Holm divides the alpha by the
    family — a requirement stated without it is a requirement for a different test.
    """
    if survivors <= 0:
        raise ValueError(
            f"survivors must be at least 1, got {survivors} — Holm corrects over a FAMILY, so a "
            + "Stage B with no candidates has no threshold to state rather than an easier one"
        )
    # Both caller errors, and both are why the rendered `None` can name ONE remedy honestly:
    # `min_discordant_rows` also returns `None` for an empty suite, where "shrink the family" is the
    # wrong advice. Rejecting that here leaves the family and the draw count as the only cause left.
    if rows <= 0:
        raise ValueError(f"rows must be at least 1, got {rows} — an empty suite has no discordant count to state")
    threshold = DEFAULT_ALPHA / survivors
    return render_discreteness(
        min_discordant_rows(rows, threshold), rows=rows, survivors=survivors, threshold=threshold
    )


def row_matrix_report(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None = None,
    n_replicates: int = 1,
) -> str:
    """The row x arm matrix and both fronts — Stage A's shortlist, never a measurement.

    ``criterion_index=None`` reads each row's ``weighted_score`` (the execution track); an index
    reads that criterion's score (the activation track). Stated once here rather than in a comment
    on every call, which is how the fences carried it.

    ``n_replicates`` is what the round was run at, and it is rendered rather than inferred: at one
    the block prints the caveat that this matrix RANKS and does not MEASURE. It defaults to 1
    because that is what Stage A costs.
    """
    _require_run_dirs(run_dirs)
    stale, _unknown = reconcile_arms([(vid, run_dirs) for vid in variant_ids], suite_id)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    return "\n".join(
        [
            *_staleness_note(stale),
            render_row_matrix(
                arms,
                pareto_front(arms),
                instance_best=instance_best_front(arms),
                n_replicates=n_replicates,
            ),
        ]
    )


def cost_quality_report(
    *, run_dirs: Sequence[Path], variant_ids: Sequence[str], suite_id: str, criterion_index: int | None = None
) -> str:
    """The quality x cost plane beside the row matrix — advisory, and never a second gate.

    Read from the SAME run dir as the matrix: pooling two passes mixes arm sets and row sets, and
    the front's coverage rule gates domination on the row count, so a second-pass arm would look
    better-evidenced for a reason that is an artefact of the procedure.

    ``criterion_index`` means what it means on :func:`row_matrix_report`.
    """
    _require_run_dirs(run_dirs)
    stale, _unknown = reconcile_arms([(vid, run_dirs) for vid in variant_ids], suite_id)
    points = cost_quality_points(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    return "\n".join([*_staleness_note(stale), render_cost_quality(points, cost_quality_front(points))])


def _staleness_note(stale: dict[str, frozenset[tuple[str, str]]]) -> list[str]:
    """The contamination warning, as block lines rather than a log record.

    :func:`arm_row_scores` can only ``logger.warning`` this, because ``ArmRowScores`` has nowhere to
    put a refusal — and a skill session never sees a warning. A composite returns markdown, which
    HAS somewhere to put it, so the staleness reaches the ledger a reader keeps instead of a stderr
    line nobody read.

    **Every composite that computes a reported number from rows it read owes the block this**, which
    is why the sweep runs here rather than being left to whichever primitive happens to do one. It
    costs one extra ``run.json`` parse per (arm, dir) — the readers below sweep again for their own
    logging — and that is the price of the note being in the ledger instead of on stderr.
    """
    if not stale:
        return []
    return [render_staleness_note(stale_tree_reason(stale)), ""]


def headroom_report(
    *, run_dirs: Sequence[Path], variant_id: str, suite_id: str, grader_index: int, sidecar: Path
) -> str:
    """What each grader rule could move the suite mean by at MOST, against the floor to be cleared.

    The one block that can say STOP before a candidate is written: a rule whose ceiling is below the
    suite's noise floor is a **suite gap, not a hypothesis**, and no wording of a candidate fixes it.
    Measured on a real round, three of four rules were unpromotable by arithmetic and roughly $40 was
    spent gating candidates for them — off inputs already paid for.

    ``grader_index`` is the POSITION of the grader's ``run_command`` criterion in the suite's
    ``success_criteria``, which is where the ``RULES`` lines come from. Get it wrong and attribution
    comes back empty; the block says so rather than rendering an empty table, because an empty table
    reads as "no rule has any headroom" when it means "nobody asked".

    Advisory, always — the attribution is AUTHORED, so a mistyped rule id moves rows between rules.
    """
    _require_run_dirs(run_dirs)
    # CE053, and the reason this is here rather than left to `arm_row_scores`: that function warns
    # and continues, because its return type has nowhere to put a refusal. The ceilings table IS a
    # reported number, so the contamination has to reach the block.
    #
    # **It is not free.** This function reads the tree THREE times — here, inside `arm_row_scores`,
    # and inside `measure_execution_noise_floor`'s own preflight — and each read globs every row dir
    # and re-validates every `task.json`. Loading once and threading the rows into the readers below
    # would remove two of the three, but that is a signature change on a rank-3 primitive; it is
    # recorded in `.claude/harness-candidates.md` rather than smuggled in here.
    stale, _unknown = reconcile_arms([(variant_id, run_dirs)], suite_id)
    arms = arm_row_scores(run_dirs=run_dirs, variant_ids=[variant_id], suite_id=suite_id)
    if not arms[0].row_scores:
        raise ValueError(
            f"{variant_id!r} scored no rows of {suite_id!r} under {', '.join(str(d) for d in run_dirs)} — "
            + "a wrong suite_id, variant id or run dir, not a result. There is no headroom to size."
        )
    rows = load_arm_rows(run_dirs, variant_id, suite_id)
    attribution = rule_row_map(rows, grader_index)
    floor = measure_execution_noise_floor(
        run_dirs=run_dirs,
        variant_id=variant_id,
        suite_id=suite_id,
        model=resolve_arm_model(run_dirs, variant_id, suite_id) or UNRESOLVED_MODEL,
        measurements=load_measurements(sidecar),
    )
    # EVERY rule the graders mentioned, not only the ones that failed: a rule this suite always
    # passes has a real ceiling of 0.0 — "no candidate for it can show anything here" — and leaving
    # it out of the table is the difference between an answer and a missing row.
    ceilings = [
        headroom_ceiling(arms[0].row_scores, rule=rule, rows=attribution.failed[rule])
        for rule in sorted(attribution.failed)
    ]
    unavailable: list[str] = []
    if not ceilings:
        # No attribution at all: the SUITE-level ceiling rather than an empty table. Both causes are
        # named because they have different remedies and the block cannot tell them apart.
        ceilings = [headroom_ceiling(arms[0].row_scores)]
        unavailable = [render_attribution_unavailable(grader_index), ""]
    # `floor is None` is passed through, never substituted with 0.0: a fabricated floor turns every
    # ceiling into a verdict, and round 1 has no floor by construction (one replicate cannot split
    # against itself).
    return "\n".join(
        [
            *_staleness_note(stale),
            *unavailable,
            render_headroom_ceilings(
                ceilings,
                None if floor is None else floor.mde,
                unattributed=len(attribution.unattributed),
            ),
        ]
    )


def corpus_report(
    *,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int | None,
    sidecar: Path,
    threshold: float = 1.0,
) -> str:
    """Which shortlisted arms re-lose a row an earlier promotion was built on.

    Read against the same arms the row matrix printed, and before shortlisting: a candidate that
    gives back a corpus row is a regression however good its aggregate looks, and an aggregate is
    exactly what cannot show it.

    ``threshold`` is surfaced rather than fixed at 1.0 because a fractional execution suite needs a
    different bar — at 1.0 any partial score is a loss, which is right for a binary activation
    criterion and wrong for a graded one.

    **``criterion_index`` is REQUIRED here**, alone among these composites, and the asymmetry is the
    point: it decides what "lost" MEANS. Defaulted to ``None`` it would silently read
    ``weighted_score`` on an activation suite and report rows the arm actually passed as corpus
    losses. On the execution track pass ``criterion_index=None`` explicitly — omitting it is a
    ``TypeError``, which is the intended answer to "which metric?" being left unanswered.
    """
    _require_run_dirs(run_dirs)
    corpus = load_measurements(sidecar).regression_corpus
    if not corpus:
        # No sweep and no load: this path computes no number from rows, so there is nothing to doubt
        # and nothing to pay for. The renderer owns the wording, beside the no-ARMS case it is not.
        return render_corpus_check({}, threshold=threshold, corpus_size=0)
    stale, _unknown = reconcile_arms([(vid, run_dirs) for vid in variant_ids], suite_id)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    # Built in `variant_ids` order, which is the render order: the incumbent first, as the shortlist
    # is read. `regression_check` reports a hole as `(row, None)` rather than skipping it — not
    # measuring a row is not passing it — and the renderer is what keeps that apart from a loss.
    return "\n".join(
        [
            *_staleness_note(stale),
            render_corpus_check(
                {arm.variant_id: regression_check(corpus, arm, threshold=threshold) for arm in arms},
                threshold=threshold,
                corpus_size=len(corpus),
            ),
        ]
    )
