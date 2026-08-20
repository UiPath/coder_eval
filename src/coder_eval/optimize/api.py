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

Why composites rather than a facade, why every one returns ``str``, why Stage C recomputes, and why
the tracks get two functions instead of a ``track:`` literal:
.claude/decisions/2026-08-20-the-skill-facing-api.md.

**Run directories are ``Path``, and that is enforced rather than documented.** A bare string is a
``Sequence`` too, so it iterates into characters and fails somewhere downstream — after the runs it
was meant to read have been paid for. Every entry point rejects one at the boundary.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from coder_eval.models import (
    ACTIVATION_FLOOR_METRIC,
    EXECUTION_FLOOR_METRIC,
    ActivationGateVerdict,
    ArmRowScores,
    ExecutionGateVerdict,
    GateVerdictBase,
    OptimizeMeasurements,
    RegressionRow,
    RoundScores,
    TaskDefinition,
)
from coder_eval.optimize.activation import (
    activation_gate,
    confirm_gate,
    gate_seed_stability,
    holm_promote,
    measure_noise_floor,
    min_discordant_rows,
    noise_floor_mde,
)
from coder_eval.optimize.execution import (
    confirm_gate_execution,
    execution_gate,
    holm_promote_execution,
    measure_execution_noise_floor,
    resolve_arm_model,
)
from coder_eval.optimize.fronts import (
    arm_row_scores,
    cost_quality_front,
    cost_quality_points,
    headroom_ceiling,
    instance_best_front,
    lineage_head,
    pareto_front,
)
from coder_eval.optimize.gate import confirm_one_candidate
from coder_eval.optimize.load import (
    load_arm_rows,
    reconcile_arms,
    row_replicate_scores,
    rule_row_map,
    stale_tree_reason,
    wrong_path_reason,
)
from coder_eval.optimize.search import (
    candidate_leaks,
    lineage_head_scores,
    regression_check,
    search_compare,
    skill_text,
)
from coder_eval.optimize.store import (
    UNRESOLVED_MODEL,
    append_regression_rows,
    grader_changed,
    load_measurements,
    record_noise_floor,
    record_round_scores,
    suite_changed,
)
from coder_eval.orchestration.task_loader import expand_dataset, load_task
from coder_eval.reports_optimize import (
    render_attribution_unavailable,
    render_comparability,
    render_confirm_family,
    render_confirm_markdown,
    render_corpus_appended,
    render_corpus_check,
    render_cost_quality,
    render_discreteness,
    render_execution_markdown,
    render_family_shrunk,
    render_headroom_ceilings,
    render_leak_scan,
    render_markdown,
    render_noise_floor,
    render_row_matrix,
    render_row_replicates,
    render_search_comparison,
    render_seed_stability,
    render_staleness_note,
)
from coder_eval.reports_stats import DEFAULT_ALPHA
from coder_eval.suite_fingerprint import suite_fingerprint


def _require_run_dirs(run_dirs: Sequence[Path], argument: str = "run_dirs") -> None:
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

    ``argument`` names the parameter in the message. It is not decoration: the ledger writers and the
    confirm composites take TWO run-dir sequences each (a round's arms and its baseline, a gate run
    and a confirm run), and "run_dirs must be a sequence" would name neither of them.
    """
    if isinstance(run_dirs, str | bytes | Path):
        raise TypeError(
            f"{argument} must be a sequence of pathlib.Path, not {type(run_dirs).__name__} — pass "
            + "[dir], not dir (a string would be read as one run directory per letter)"
        )
    if not run_dirs:
        raise ValueError(f"{argument} is empty — no run directory was named, so there is nothing to measure")
    wrong = [d for d in run_dirs if isinstance(d, str | bytes)]
    if wrong:
        raise TypeError(
            f"{argument} holds {len(wrong)} string(s) rather than pathlib.Path ({wrong[0]!r}) — these are "
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
    # `[0]` cannot raise — `arm_row_scores` returns one arm per variant id and this passes one. The
    # real failure is an arm that scored NOTHING, which comes back as a present arm with an empty
    # vector; that is what the fence exited the interpreter over, and what this raises on.
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


def replicates_report(
    *,
    run_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    criterion_index: int | None = None,
) -> str:
    """Per-row replicate values for both arms — the two readings a suite mean cannot give.

    A row with zero variance on BOTH arms and a non-zero delta is a REPRODUCIBLE behavioural change,
    and the most informative row in a run: measured, two of them with opposite signs cancelled to a
    suite delta of +0.0001, where "the difference is noise" is the opposite of what happened. A row
    whose mean delta is exactly 0.0 is dead for the comparison, and a suite of dead rows resolves
    nothing however many rows it has.

    Read it BESIDE the verdict, never instead of it: the verdict block has no channel for either.
    """
    _require_run_dirs(run_dirs)
    if incumbent_variant == candidate_variant:
        # An arm against itself renders every delta as 0.000 and every row as dead, which reads as
        # "this candidate changes nothing" — a result, for what is a typo. `execution_gate` refuses
        # the same comparison for the same reason; a report has no less need of the guard.
        raise ValueError(
            f"incumbent_variant and candidate_variant are both {incumbent_variant!r} — an arm compared "
            + "against itself renders every row dead, which is not a reading of a candidate"
        )
    # ONE sweep over both arms rather than two. `fronts.py`'s own comment gives the reason: a dir
    # carrying both arms reports one warning naming both, rather than one per arm for what is a
    # single re-used `--run-dir`. The block gets the same treatment, for the same reason.
    stale, _unknown = reconcile_arms([(incumbent_variant, run_dirs), (candidate_variant, run_dirs)], suite_id)
    incumbent = row_replicate_scores(load_arm_rows(run_dirs, incumbent_variant, suite_id), criterion_index)
    candidate = row_replicate_scores(load_arm_rows(run_dirs, candidate_variant, suite_id), criterion_index)
    # An arm that scored NOTHING renders as a full column of `— (hole)`, and the block's own prose
    # then tells the reader that means "present on one arm only" — a reading, for a mistyped variant
    # or run dir. Every sibling makes this loud (`headroom_report` raises, `render_row_matrix` names
    # the arm), so this one does too, and rank 0 owns the wording.
    empty = [v for v, scores in ((incumbent_variant, incumbent), (candidate_variant, candidate)) if not scores]
    return "\n".join(
        [
            *_staleness_note(stale),
            *([wrong_path_reason(v, suite_id, run_dirs) for v in empty] + [""] if empty else []),
            render_row_replicates(incumbent, candidate),
        ]
    )


def leak_report(*, suite: Path, skill_name: str, root: Path, round_tag: str, baseline_dir: Path) -> str:
    """Train-row content a round's candidates newly reproduce verbatim — before Stage A is paid for.

    A candidate that reproduces a train row's graded content scores well on that row whether or not
    the behaviour under test happened, and it teaches the skill nothing. Static: no runs, so it costs
    nothing and belongs before the first stage rather than after one.

    **There is no ``split`` parameter, and that is deliberate.** It scans the TRAIN rows and only
    those. Scanning the whole suite flags content a candidate is entitled to be fitted to; scanning
    the test rows reports on a split the proposer is blinded to. Neither is a knob worth having.

    ``baseline_dir`` stays explicit because a candidate is diffed against **what it was edited
    from**, which is not always this round's incumbent: from round 2 a search-loop candidate is built
    on the lineage head, whose snapshot lives under the round that produced it. Defaulting this would
    re-report every span the head added, on every round — the wolf-crying the diff exists to prevent.

    Each arm's WHOLE skill directory is read, never one file: a graded string bundled into
    ``scripts/`` or a reference file is invisible to a one-file read, which comes back clean.
    """
    task, _raw = load_task(suite)
    train = expand_dataset(task, suite.parent, split="train")
    baseline_skill = baseline_dir / "skills" / skill_name
    if not baseline_skill.is_dir():
        # RAISES, where a missing CANDIDATE dir is only named. The asymmetry is the point: a missing
        # baseline makes `skill_text` return "", which degrades the diff into an ABSOLUTE scan — every
        # graded string in every arm reported as newly added. That is the wolf-crying the diff exists
        # to prevent, and it is silent, so it cannot be a rendered note.
        raise ValueError(
            f"no baseline skill directory at {baseline_skill} — without it every candidate is diffed "
            + "against an empty string, which reports every graded string as newly added. Name the "
            + "snapshot the candidates were edited FROM (on a search round, the lineage head's)."
        )
    baseline = skill_text(baseline_skill)
    # By NAME, as the fence does: the baseline is what everything is diffed against, and the control
    # arm is not a candidate. Named in the block so a reader counting arms knows why the count is short.
    skipped = sorted({baseline_dir.name, f"{round_tag}-control"})
    findings: dict[str, list[str]] = {}
    unscannable: dict[str, str] = {}
    for arm in sorted(p for p in root.glob(f"{round_tag}-*") if p.is_dir() and p.name not in skipped):
        skill_dir = arm / "skills" / skill_name
        if not skill_dir.is_dir():
            # Named and continued rather than raised: one mis-snapshotted arm must not hide what the
            # others are carrying, which is the whole output of this preflight. It goes in its own
            # channel, because a wiring fault reported as a leak SPAN reads as memorization.
            unscannable[arm.name] = f"no skill directory at {skill_dir}"
            continue
        findings[arm.name] = candidate_leaks(skill_text(skill_dir), baseline, train)
    return render_leak_scan(findings, skipped=skipped, unscannable=unscannable)


def search_report(
    *,
    run_dirs: Sequence[Path],
    variant_id: str,
    suite_id: str,
    sidecar: Path,
    criterion_index: int | None = None,
) -> str:
    """The search loop's accept-or-revert reading for one round's single explored arm.

    **Emphatically not a gate.** It compares one arm against the recorded lineage head, where the
    alternative is reverting a step rather than shipping a skill, and it corrects for no
    multiplicity. The gates are Stage B's, and both go through Holm.

    ``search_compare`` applies four guards that are easy to leave out and silent when they are — an
    empty head, no shared rows, a hole the head scored, and a corpus row the candidate re-loses — so
    the block is the whole answer. Print it and act on what it says.
    """
    _require_run_dirs(run_dirs)
    measurements = load_measurements(sidecar)
    head = lineage_head_scores(measurements)
    if head is None:
        raise ValueError(
            "no recorded lineage — run a multi-arm Stage A round first. The search loop advances a "
            + "head, so there has to be one to advance."
        )
    stale, _unknown = reconcile_arms([(variant_id, run_dirs)], suite_id)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=[variant_id], suite_id=suite_id, criterion_index=criterion_index
    )
    if not arms[0].row_scores:
        # Same shape as `headroom_report`: one variant in, one arm out, so the index is safe and the
        # empty VECTOR is the fault. The shipped fence did not crash on it either — it handed the
        # empty arm to `search_compare`, which refused with "the two rounds share no rows … a wiring
        # fault", sending a reader to check sampling seeds and snapshot mounts for what is a mistyped
        # slug. Naming the variant, the suite and the dirs is the difference.
        raise ValueError(wrong_path_reason(variant_id, suite_id, run_dirs))
    return "\n".join(
        [
            *_staleness_note(stale),
            # The corpus is passed as it is, empty or not: `search_compare` takes `corpus=()` and an
            # empty one is normal early, so branching here would only be a second way to say that.
            render_search_comparison(search_compare(head, arms[0], corpus=measurements.regression_corpus)),
        ]
    )


def _reject_incumbent_as_candidate(family: Sequence[str], incumbent_variant: str, *, argument: str) -> None:
    """The incumbent is not a member of its own Stage B family, on either track.

    Shared because the CLAIM is identical and the tracks spell the family differently — a sequence of
    candidate ids on one, a candidate-to-run-dir mapping on the other. It gates an arm against itself,
    which each track reports in its own words and neither reports as a family problem, while adding a
    member to the Holm family so every REAL candidate is decided against a tighter threshold. Nothing
    in either block connects the two, and it is one copy-paste away: Stage A's ``variant_ids``
    legitimately starts with the incumbent.
    """
    if incumbent_variant in family:
        raise ValueError(
            f"{argument} contains the incumbent {incumbent_variant!r} — that gates an arm against "
            + "itself and inflates the Holm family, tightening the threshold for the real candidates. "
            + "Stage A's variant_ids includes the incumbent; a Stage B family does not."
        )


def _resolve_family(candidate_variants: Sequence[str], incumbent_variant: str) -> list[str]:
    """The Stage B family, MATERIALIZED and validated. Every rejection is one Holm family size.

    It returns the list rather than only checking, because validating a one-shot iterable and then
    iterating it again is how a guard passes and the loop below it yields nothing: the block comes
    back empty, which is the exact "reads as nothing promoted" outcome the empty check exists for.

    Three rejections, and all three end the same way — Holm dividing the alpha by a family larger
    than the one actually gated, which makes the test stricter for every REAL candidate in it:

    * A ``str`` is a ``Sequence[str]``, so ``"cand-a"`` would be one candidate per letter. The same
      hole :func:`_require_run_dirs` closes on the other argument.
    * A DUPLICATE counts one candidate twice.
    * The INCUMBENT in the candidate list gates an arm against itself. That block reads ``CANNOT
      SEPARATE`` on its own, and nothing connects it to the tightened threshold on its siblings —
      and it is one copy-paste away, because Stage A's ``variant_ids`` legitimately starts with the
      incumbent.
    """
    if isinstance(candidate_variants, str):
        raise TypeError(
            f"candidate_variants must be a sequence of variant ids, not a string ({candidate_variants!r}) — "
            + "a string would be read as one candidate per character"
        )
    family = list(candidate_variants)
    if not family:
        raise ValueError("candidate_variants is empty — an empty block reads as 'nothing promoted'")
    duplicates = sorted(name for name, count in Counter(family).items() if count > 1)
    if duplicates:
        raise ValueError(
            f"candidate_variants repeats {duplicates} — a duplicate inflates the Holm family size, "
            + "which makes the test stricter for every candidate in it, including the real ones"
        )
    _reject_incumbent_as_candidate(family, incumbent_variant, argument="candidate_variants")
    return family


def _family_shrink_note(decided: Sequence[GateVerdictBase], predeclared: int) -> list[str]:
    """The one fail-OPEN case in this area, said out loud on every surface that can produce it.

    A verdict with no p-value is not a family member — there is nothing for a correction to correct —
    so an arm that refused drops out and ``m`` falls. Right for that arm, wrong for its siblings: they
    were predeclared against the larger family and are decided against the smaller, LOOSER threshold.
    Only a caller holding the predeclared count can see it, and all four Stage B / Stage C surfaces
    hold one — including the two that also PRINT that count, where a silent shrink makes the printed
    number a false claim about the threshold the winner cleared.
    """
    in_family = sum(1 for verdict in decided if verdict.p_value is not None)
    if in_family >= predeclared:
        return []
    return [render_family_shrunk(predeclared=predeclared, corrected=in_family)]


def activation_gate_report(
    *,
    gate_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variants: Sequence[str],
    suite_id: str,
    criterion_index: int,
    sibling_indices: Sequence[int] | None = None,
) -> str:
    """Stage B on the activation track: gate every candidate, then correct ONCE over the family.

    **The ordering is the test, and here it is structural rather than remembered.**
    ``candidate_variants`` is a SEQUENCE, so there is no single-candidate call shape to get wrong:
    every verdict is built before :func:`holm_promote` sees any of them, and the correction runs once
    across the whole p-value vector. Gating one candidate at a time — calling the correction per
    verdict — is a different, weaker test that silently reverts to an uncorrected alpha, and it was
    the shipped fence's most available mistake.

    A family of ONE is legal and is not special-cased: Holm at ``m = 1`` is the uncorrected alpha by
    construction, which is the right answer for a round that gated one candidate.

    ``seed``, ``n_resamples``, ``confidence`` and ``materiality`` are deliberately NOT exposed. No
    shipped fence varies them, and every exposed knob is a way to produce a number that is not
    comparable with the floor recorded beside it.

    ``sibling_indices`` IS exposed, and the default is the safe state rather than the convenient one:
    ``None`` DERIVES every other classification position and checks it, ``()`` checks nothing, and an
    explicit sequence checks exactly those. So omitting it arms the veto — the parameter exists to let
    a caller turn the check off deliberately, which is the only reason it is reachable at all.
    """
    _require_run_dirs(gate_dirs, "gate_dirs")
    family = _resolve_family(candidate_variants, incumbent_variant)
    # NO `_staleness_note` here, alone among the reporting composites, and the reason is the return
    # type: `activation_gate` runs the sweep in its own preflight and REFUSES on a contaminated tree,
    # so the doubt arrives as the verdict's own `NOT A RESULT` headline rather than as a note beside a
    # number. Adding the sweep here would read every run.json twice to say the same thing later.
    verdicts = [
        activation_gate(
            incumbent_run_dirs=gate_dirs,
            candidate_run_dirs=gate_dirs,
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate,
            suite_id=suite_id,
            criterion_index=criterion_index,
            sibling_indices=sibling_indices,
        )
        for candidate in family
    ]
    # ONE call, over the whole family. `decide_family` inside it is what runs `holm_rejections` once
    # across the p-value vector, and a refusal that left `promoted=None` is forced to False here — so
    # the block a reader acts on is never an undecided verdict.
    decided = holm_promote(verdicts)
    return "\n\n".join([*_family_shrink_note(decided, len(family)), *map(render_markdown, decided)])


def seed_stability_report(
    *,
    gate_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    criterion_index: int,
    sibling_indices: Sequence[int] | None = None,
    seeds: Sequence[int] = (0, 1, 2),
) -> str:
    """Whether one candidate's decision survives the bootstrap seed. Costs no runs at all.

    Three bootstraps over rows already on disk, so it is CPU and nothing else. Disagreeing seeds are
    the FINDING, not an error: a 2/3 split means the decision is being made by the draw count rather
    than by the data, and the block presents no single verdict to mistake for the answer.

    Every keyword is spelled out rather than forwarded as a bag. :func:`gate_seed_stability` takes
    ``**gate_kwargs`` and that stays the internal seam, but an untyped bag on this surface is exactly
    what a declared API is not — pyright cannot check it, and the skill's snippet binder cannot
    either.
    """
    _require_run_dirs(gate_dirs, "gate_dirs")
    if incumbent_variant == candidate_variant:
        # `SeedStability` has no refusal channel, so a self-comparison renders as
        # "STABLE — would promote at none of 3 seeds": a maximally confident negative for what is a
        # typo. `replicates_report` refuses the same comparison for the same reason.
        raise ValueError(
            f"incumbent_variant and candidate_variant are both {incumbent_variant!r} — an arm gated "
            + "against itself reads as a stable negative rather than as the wiring fault it is"
        )
    duplicate_seeds = sorted(seed for seed, count in Counter(seeds).items() if count > 1)
    if duplicate_seeds:
        # Re-running ONE draw three times reports 3/3 agreement at a spread of 0.0000 — the most
        # confident stability claim available, from a single bootstrap. The seeds ARE the axis.
        raise ValueError(
            f"seeds repeats {duplicate_seeds} — re-running one draw reports perfect agreement at zero "
            + "spread, which is a confident claim with no evidence behind it"
        )
    return render_seed_stability(
        gate_seed_stability(
            seeds=seeds,
            incumbent_run_dirs=gate_dirs,
            candidate_run_dirs=gate_dirs,
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate_variant,
            suite_id=suite_id,
            criterion_index=criterion_index,
            sibling_indices=sibling_indices,
        )
    )


def _require_gates(gates: Mapping[str, Path]) -> None:
    """Reject a mapping that is not one, and values that are not ``Path``, before any read.

    The execution track's family is a MAPPING, so two of the three shape mistakes
    :func:`_require_run_dirs` catches are unrepresentable here — a duplicate key cannot exist and
    there is no one-shot iterable to exhaust. What remains is the container itself (a ``str`` or a
    list has no ``.items()``) and the values (a ``str`` where a ``Path`` belongs). Neither is silent
    today, but both surface as an ``AttributeError`` from whichever line reaches them first, naming
    neither the argument nor the fix — and the module's own contract says every entry point rejects a
    bad path AT the boundary.
    """
    if not isinstance(gates, Mapping):
        raise TypeError(
            f"gates must be a mapping of candidate id -> run directory, not {type(gates).__name__} — "
            + "this track gates one candidate per two-variant run dir, so the mapping IS the family"
        )
    wrong = sorted(f"{candidate}={value!r}" for candidate, value in gates.items() if not isinstance(value, Path))
    if wrong:
        raise TypeError("gates values must be pathlib.Path run directories: " + "; ".join(wrong))


def _require_gate_dirs_exist(gates: Mapping[str, Path]) -> None:
    """Every candidate's gate run dir exists, named per candidate when one does not.

    RAISES rather than letting the gate refuse per arm, which it would do perfectly well — its
    refusal names the missing ``experiment.json``. The trade is deliberate and matches
    :func:`headroom_report` and :func:`leak_report`: a run dir that does not exist is a wiring fault
    in the round's own plumbing rather than a measurement. The cost is that one bad dir aborts the
    whole block instead of reporting the other candidates' already-paid-for verdicts — which is the
    right way round on this track, because a partial family is exactly what shrinks ``m`` and loosens
    the threshold for the arms that did run.
    """
    missing = sorted(f"{candidate} -> {run_dir}" for candidate, run_dir in gates.items() if not run_dir.is_dir())
    if missing:
        raise ValueError(
            "no gate run directory for "
            + "; ".join(missing)
            + " — each candidate is gated in its own two-variant round, so each has its own"
            + " --run-dir to name"
        )


def execution_gate_report(
    *,
    gates: Mapping[str, Path],
    incumbent_variant: str,
    suite_id: str,
    engagement_criterion_index: int | None = 0,
    primary_criterion_index: int | None = None,
) -> str:
    """Stage B on the execution track: gate every candidate, then correct ONCE over the family.

    **The mapping IS the family**, and that is why this signature differs from the activation twin's.
    ``paired_comparison`` fires only for exactly two variants, so each candidate is gated in its own
    two-variant round, and the Holm family therefore lives ACROSS run dirs rather than inside one.
    Candidate id to that candidate's gate run dir is the only shape that can express it.

    The block order is by candidate id — see the comment at the sort for why this track differs from
    the twin.

    ``primary_criterion_index=None`` is the normal case and stays a READING rather than a decision —
    it is passed through untouched.

    ``engagement_criterion_index`` is the other kind of parameter entirely, and the default is the
    safe state: ``0`` checks that the candidate still engages the skill, and ``None`` **disarms that
    veto** (the engagement reading feeds ``integrity_checks``, and a failed one forces ``promoted``
    False). It is exposed for a suite whose engagement criterion sits elsewhere — not as a knob, and
    never as a way to quiet a failing check. Same shape as the activation twin's ``sibling_indices``.

    No estimator knob is exposed, for the reason the activation twin gives.
    """
    # NO `_staleness_note`, for the twin's reason: `execution_gate`'s own preflight runs
    # `_refuse_stale_tree` and REFUSES, so a contaminated tree arrives as the verdict's own
    # `NOT A RESULT` headline rather than as a note beside a number.
    _require_gates(gates)
    if not gates:
        raise ValueError("gates is empty — an empty block reads as 'nothing promoted'")
    _reject_incumbent_as_candidate(list(gates), incumbent_variant, argument="gates")
    _require_gate_dirs_exist(gates)
    verdicts = [
        execution_gate(
            run_dir=gates[candidate],
            incumbent_variant=incumbent_variant,
            candidate_variant=candidate,
            suite_id=suite_id,
            engagement_criterion_index=engagement_criterion_index,
            primary_criterion_index=primary_criterion_index,
        )
        # Sorted, where the activation twin deliberately renders the caller's list in the order
        # given. The difference is what the two containers mean: a LIST of candidates is authored
        # once, in the round's proposal order, and reordering it would stop a ledger entry lining up
        # with that list. A MAPPING is routinely rebuilt — a comprehension, a config, a JSON
        # round-trip — so its insertion order is an artefact rather than a statement, and sorting is
        # what makes two rounds' blocks comparable. It changes no number: the correction is
        # order-independent.
        for candidate in sorted(gates)
    ]
    # ONE call over the whole family, as on the other track — and a refusal from any single arm keeps
    # `promoted=None` until this forces False, so it reaches the block either way.
    decided = holm_promote_execution(verdicts)
    return "\n\n".join([*_family_shrink_note(decided, len(gates)), *map(render_execution_markdown, decided)])


def _select_stage_b_winner[V: GateVerdictBase](decided: Sequence[V], candidate_variant: str) -> V:
    """The Stage B verdict Stage C is about, refused only when there is nothing to confirm.

    **It does NOT refuse a candidate that merely lost**, and that boundary is rank 1's rather than
    this module's: :func:`~coder_eval.optimize.gate.confirm_train_note` says in writing that a reader
    may legitimately want to confirm a candidate that separated and was then vetoed by a guardrail,
    and the confirm gate NOTES such a train verdict rather than rejecting it. A rank-4 composite whose
    contract is that it decides nothing must not decide that the other way.

    What it does refuse is a verdict with **no statistic at all** — a gate that could not measure.
    That is not a candidate that lost, it is one nothing was learned about, and the causes want
    different fixes: a wrong ``criterion_index``, a wrong ``suite_id`` or run dir, or a refusal the
    gate already worded. Reporting any of them as "did not promote" sends a reader to rewrite a
    candidate whose gate never ran.
    """
    by_variant = {verdict.candidate_variant: verdict for verdict in decided}
    if candidate_variant not in by_variant:
        raise ValueError(
            f"{candidate_variant!r} is not in the Stage B family {sorted(by_variant)} — Stage C "
            + "recomputes that family to recover the corrected verdict, so the candidate has to be "
            + "one of its members"
        )
    winner = by_variant[candidate_variant]
    if winner.p_value is None:
        raise ValueError(
            f"the Stage B gate for {candidate_variant!r} produced no statistic, so there is nothing "
            + "to confirm — this is NOT a candidate that lost. "
            + (
                f"The gate refused: {winner.gate_refusal}"
                if winner.gate_refusal
                else (
                    "Check `criterion_index`, `suite_id` and the gate run dirs: a gate that read no"
                    + " comparable rows returns no p-value."
                )
            )
        )
    return winner


def _track_verdict[V: GateVerdictBase](verdict: GateVerdictBase, expected: type[V], track_name: str) -> V:
    """Narrow ``ConfirmVerdict.test_verdict`` to the track that produced it.

    The field is the union of both tracks' verdicts, because ``ConfirmVerdict`` is deliberately ONE
    model over both. A confirm gate on one track cannot produce the other's verdict, so this is a
    narrowing rather than a branch — but it raises rather than casting, because a silent
    ``cast`` here would render the wrong track's block for a caller who mixed the two up.
    """
    if not isinstance(verdict, expected):
        raise TypeError(
            f"the {track_name} confirm gate returned a {type(verdict).__name__} test verdict — the two "
            + "tracks' verdicts are not interchangeable and their blocks are not either"
        )
    return verdict


def confirm_report_activation(
    *,
    gate_dirs: Sequence[Path],
    confirm_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variants: Sequence[str],
    candidate_variant: str,
    suite_id: str,
    criterion_index: int,
    sibling_indices: Sequence[int] | None = None,
) -> str:
    """Stage C on the activation track: did the Stage B effect REPRODUCE on the held-out split?

    **It recomputes Stage B rather than being handed a verdict**, and that is a deliberate trade.
    ``confirm_gate`` needs the HOLM-CORRECTED verdict, because ``promoted`` is what Stage C
    classifies against — and ``measurements.json`` is ``extra="forbid"`` with nowhere to put one. The
    bootstrap is seeded, so gating the same family over the same trees is bit-identical; the cost is
    CPU over rows already on disk. It also removes the failure the skill's own prose warned about,
    where the fence needed a `promoted_verdict` name from an earlier snippet and raised `NameError`
    in a fresh interpreter after the round had been paid for.

    ``gate_dirs`` + ``candidate_variants`` are the Stage B family; ``candidate_variant`` is the one
    being confirmed. ``confirm_dirs`` is the confirm run, and it feeds both arms — one two-variant
    round, as the fence does.
    """
    # BEFORE the recomputation, which costs one full bootstrap per family member: a shortlist here
    # would otherwise die on an unhashable dict key after the whole family had been re-gated. Rank 1
    # owns the sentence, and `SKILL.md` advertises this guard by name.
    confirm_one_candidate(candidate_variant)
    _require_run_dirs(gate_dirs, "gate_dirs")
    _require_run_dirs(confirm_dirs, "confirm_dirs")
    family = _resolve_family(candidate_variants, incumbent_variant)
    # Recomputed, not persisted — see the docstring. Seeded, so this is the same verdict Stage B
    # rendered, and the family size goes into the block because nothing on disk records it.
    decided = holm_promote(
        [
            activation_gate(
                incumbent_run_dirs=gate_dirs,
                candidate_run_dirs=gate_dirs,
                incumbent_variant=incumbent_variant,
                candidate_variant=candidate,
                suite_id=suite_id,
                criterion_index=criterion_index,
                sibling_indices=sibling_indices,
            )
            for candidate in family
        ]
    )
    confirm = confirm_gate(
        train_verdict=_select_stage_b_winner(decided, candidate_variant),
        incumbent_run_dirs=confirm_dirs,
        candidate_run_dirs=confirm_dirs,
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        criterion_index=criterion_index,
        sibling_indices=sibling_indices,
    )
    return "\n\n".join(
        [
            render_confirm_family(family_size=len(family)),
            render_confirm_markdown(confirm),
            render_markdown(_track_verdict(confirm.test_verdict, ActivationGateVerdict, "activation")),
        ]
    )


def confirm_report_execution(
    *,
    gates: Mapping[str, Path],
    confirm_run_dir: Path,
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    engagement_criterion_index: int | None = 0,
    primary_criterion_index: int | None = None,
) -> str:
    """Stage C on the execution track — the same recomputation, on this track's family shape.

    ``gates`` is Stage B's mapping, candidate id to that candidate's gate run dir, because this
    track's Holm family lives across run dirs. See :func:`confirm_report_activation` for why the
    family is recomputed rather than persisted.
    """
    confirm_one_candidate(candidate_variant)
    _require_gates(gates)
    if not gates:
        raise ValueError("gates is empty — Stage C recomputes the Stage B family, so there has to be one")
    _reject_incumbent_as_candidate(list(gates), incumbent_variant, argument="gates")
    _require_gate_dirs_exist(gates)
    if not isinstance(confirm_run_dir, Path):
        raise TypeError(
            f"confirm_run_dir must be a pathlib.Path, not {type(confirm_run_dir).__name__} — it is "
            + "joined with `/` to reach the confirm run's rows"
        )
    decided = holm_promote_execution(
        [
            execution_gate(
                run_dir=gates[candidate],
                incumbent_variant=incumbent_variant,
                candidate_variant=candidate,
                suite_id=suite_id,
                engagement_criterion_index=engagement_criterion_index,
                primary_criterion_index=primary_criterion_index,
            )
            for candidate in sorted(gates)
        ]
    )
    shrunk = _family_shrink_note(decided, len(gates))
    confirm = confirm_gate_execution(
        train_verdict=_select_stage_b_winner(decided, candidate_variant),
        confirm_run_dir=confirm_run_dir,
        incumbent_variant=incumbent_variant,
        candidate_variant=candidate_variant,
        suite_id=suite_id,
        engagement_criterion_index=engagement_criterion_index,
        primary_criterion_index=primary_criterion_index,
    )
    return "\n\n".join(
        [
            *shrunk,
            render_confirm_family(family_size=len(gates)),
            render_confirm_markdown(confirm),
            render_execution_markdown(_track_verdict(confirm.test_verdict, ExecutionGateVerdict, "execution")),
        ]
    )


def _previous_round(measurements: OptimizeMeasurements, round_number: int) -> RoundScores | None:
    """The round before this one, from a snapshot taken BEFORE the write.

    Two halves, and both were bugs in the fence this replaces. Reading AFTER the write compares the
    round against itself and prints "same grader" forever. And excluding the CURRENT round number is
    what handles a re-run: ``record_round_scores`` replaces per round, so the stored entry for this
    round is an earlier version of itself rather than the one before it.
    """
    earlier = [r for r in measurements.round_scores if r.round < round_number]
    return max(earlier, key=lambda r: r.round, default=None)


def _require_scored_arms(arms: Sequence[ArmRowScores], variant_ids: Sequence[str], suite_id: str) -> None:
    """No arm scored anything — a mistyped id, not a round.

    Writing it anyway records empty fronts, no lineage head, and a suite digest over ZERO rows — and
    the NEXT round then reports "The SUITE CHANGED" for a suite nobody touched. Every sibling raises
    on this input; the ledger is the one place where the consequence outlives the call.
    """
    if not any(arm.row_scores for arm in arms):
        raise ValueError(
            f"none of {list(variant_ids)} scored a row of {suite_id!r} — a wrong suite id, variant id "
            + "or run dir, not a round. Recording it would write a suite digest over zero rows, and "
            + "the next round would report the suite as CHANGED."
        )


def _scored_row_tasks(suite_file: Path, arms: Sequence[ArmRowScores]) -> tuple[TaskDefinition, list[TaskDefinition]]:
    """The unexpanded suite task and the expanded rows the round actually scored.

    The two are different things and :func:`~coder_eval.suite_fingerprint.suite_fingerprint` takes
    them separately for that reason. The scored ids are the **union** across arms, never ``arms[0]``:
    a hole is absent rather than zero, so an arm that crashed a row has a shorter vector and reading
    one arm would make the digest a function of which arm happens to be first.
    """
    task, _raw = load_task(suite_file)
    scored = {row_id for arm in arms for row_id in arm.row_scores}
    rows = [r for r in expand_dataset(task, suite_file.parent) if r.row_id in scored]
    return task, rows


# What the bundled grader's `--fingerprint` mode emits: a SHA-256 hexdigest. Validated rather than
# trusted, and the instrument's own docstring is why — "there is no score here, and a score-shaped
# line would be recorded by a caller as the fingerprint itself". A grader predating the flag treats
# `--fingerprint` as a row id, prints a score line and exits 0, so `check=True` catches nothing: the
# recorded identity becomes a string that is constant while the real grader moves, and carries an
# absolute path that differs per machine.
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _grader_fingerprint(suite_file: Path, task: TaskDefinition) -> str:
    """The execution grader's own fingerprint, resolved from the SUITE rather than a typed path.

    It is the ``run_command`` criterion's own command, with ``$TASK_DIR`` standing for the suite
    file's directory — so a suite whose grader moved cannot leave this pointing at the old script.
    ``--fingerprint`` hashes the script AND the expectations it loads, because the answer key is part
    of the instrument.

    ``check=True`` with an argument list, never a shell string, and the output is checked against the
    digest SHAPE: the grader exits non-zero only in this mode, so an exit code alone does not
    distinguish a fingerprint from a score line.
    """
    commands = [c.command for c in task.success_criteria if c.type == "run_command"]
    if not commands:
        raise ValueError(
            f"{suite_file} declares no `run_command` criterion, so there is no script grader to "
            + "fingerprint. On the activation track that is expected — use record_round_activation."
        )
    if len(commands) > 1:
        # The grader's POSITION is not discoverable — `headroom_report` takes an explicit
        # `grader_index` for the same suite — so picking the first silently fingerprints a build step.
        raise ValueError(
            f"{suite_file} declares {len(commands)} `run_command` criteria, so which one is the "
            + "grader is ambiguous. The fingerprint identifies ONE instrument; split the others out."
        )
    # `shlex.split`, because the real runner honours quoting and a parser that does not would read
    # `"$TASK_DIR/my grader/verify.py"` as a path ending at the space.
    tokens = [t for t in shlex.split(commands[0]) if "$TASK_DIR" in t]
    if len(tokens) != 1:
        raise ValueError(
            f"{suite_file}'s grader command names {len(tokens)} `$TASK_DIR` token(s) in "
            + f"{commands[0]!r} — the grader script is the one path the fingerprint hashes, so "
            + "exactly one is expected"
        )
    grader = Path(tokens[0].replace("$TASK_DIR", str(suite_file.parent)))
    completed = subprocess.run(
        [sys.executable, str(grader), "--fingerprint"],
        capture_output=True,
        text=True,
        # CE010, and it matters more here than usual: this string becomes the round's recorded
        # instrument identity, so a locale-dependent decode would make the SAME grader fingerprint
        # differently on a Windows checkout and read as "the grader changed".
        encoding="utf-8",
        check=True,
    )
    digest = completed.stdout.strip()
    if not _FINGERPRINT_PATTERN.match(digest):
        raise ValueError(
            f"{grader} --fingerprint printed {digest[:80]!r}, which is not a SHA-256 digest. A grader "
            + "predating that flag reads it as a row id and prints a SCORE line, which would be "
            + "recorded as this round's instrument identity — constant while the grader moves, and "
            + "different on every machine."
        )
    return digest


def record_round_activation(
    *,
    sidecar: Path,
    round_number: int,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    criterion_index: int,
    baseline_dirs: Sequence[Path],
    suite_file: Path,
    baseline_variant_id: str = "default",
    lineage_head_variant: str | None | Literal["auto"] = "auto",
) -> str:
    """Write this round's activation-track measurements, and report what they are comparable with.

    A ``record_*`` name, so it WRITES: the round's row vectors and fronts, and the ``f1.yes`` noise
    floor when one could be measured.

    **There is no grader-fingerprint parameter here, and that is the point.** The activation track has
    no script grader, so the combination is unrepresentable rather than asserted against — which is
    also why the comparability sentence says "no script grader" instead of "comparability unknown", a
    wording the un-branched fence printed on every round of this track forever.

    ``lineage_head_variant`` has THREE states, and the third is why it is not just ``str | None``.
    ``"auto"`` (the default) derives the head from this round's arms via
    :func:`~coder_eval.optimize.fronts.lineage_head`, which is right for a multi-arm round. A variant
    id records that arm. And **``None`` records NO head**, which is what a REVERTED search round
    needs: a search round has one arm, so deriving would name the rejected candidate as the head and
    advance the bar every later round is measured against. ``None`` leaves the lineage where it was —
    :func:`~coder_eval.optimize.search.lineage_head_scores` skips a round that named none.
    """
    _require_run_dirs(run_dirs)
    _require_run_dirs(baseline_dirs, "baseline_dirs")
    # ONE load, and `previous` comes off THIS snapshot — before the write. See `_previous_round`.
    measurements = load_measurements(sidecar)
    previous = _previous_round(measurements, round_number)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    floor = measure_noise_floor(
        run_dirs=baseline_dirs,
        variant_id=baseline_variant_id,
        suite_id=suite_id,
        criterion_index=criterion_index,
        model=resolve_arm_model(baseline_dirs, baseline_variant_id, suite_id) or UNRESOLVED_MODEL,
        measurements=measurements,
    )
    _require_scored_arms(arms, variant_ids, suite_id)
    task, rows = _scored_row_tasks(suite_file, arms)
    this_round = RoundScores(
        round=round_number,
        arm_row_scores=arms,
        grader_fingerprint=None,
        suite_fingerprint=suite_fingerprint(task, rows),
        pareto_front=pareto_front(arms),
        instance_best_front=instance_best_front(arms),
        lineage_head=lineage_head(arms) if lineage_head_variant == "auto" else lineage_head_variant,
    )
    if floor is not None:
        record_noise_floor(sidecar, floor)
    record_round_scores(sidecar, this_round)
    return render_comparability(
        grader=grader_changed(previous, this_round),
        suite=suite_changed(previous, this_round),
        has_grader=False,
        has_previous=previous is not None,
    )


def record_round_execution(
    *,
    sidecar: Path,
    round_number: int,
    run_dirs: Sequence[Path],
    variant_ids: Sequence[str],
    suite_id: str,
    control_dirs: Sequence[Path],
    control_variant_id: str,
    suite_file: Path,
    criterion_index: int | None = None,
    lineage_head_variant: str | None | Literal["auto"] = "auto",
) -> str:
    """Write this round's execution-track measurements, and report what they are comparable with.

    The twin of :func:`record_round_activation`, on this track's floor (``weighted_score``, split over
    REPLICATES from the control arm) and with the one thing that track does not have: the grader
    fingerprint, resolved from the suite rather than a typed path.
    """
    _require_run_dirs(run_dirs)
    _require_run_dirs(control_dirs, "control_dirs")
    measurements = load_measurements(sidecar)
    previous = _previous_round(measurements, round_number)
    arms = arm_row_scores(
        run_dirs=run_dirs, variant_ids=variant_ids, suite_id=suite_id, criterion_index=criterion_index
    )
    _require_scored_arms(arms, variant_ids, suite_id)
    task, rows = _scored_row_tasks(suite_file, arms)
    # BEFORE the bootstrap. Its three raise paths — no `run_command`, an ambiguous one, a
    # non-fingerprint output — are all decidable from the suite file alone, and every one of them
    # would otherwise discard a floor that had already been computed.
    grader = _grader_fingerprint(suite_file, task)
    floor = measure_execution_noise_floor(
        run_dirs=control_dirs,
        variant_id=control_variant_id,
        suite_id=suite_id,
        model=resolve_arm_model(control_dirs, control_variant_id, suite_id) or UNRESOLVED_MODEL,
        measurements=measurements,
    )
    this_round = RoundScores(
        round=round_number,
        arm_row_scores=arms,
        grader_fingerprint=grader,
        suite_fingerprint=suite_fingerprint(task, rows),
        pareto_front=pareto_front(arms),
        instance_best_front=instance_best_front(arms),
        lineage_head=lineage_head(arms) if lineage_head_variant == "auto" else lineage_head_variant,
    )
    if floor is not None:
        record_noise_floor(sidecar, floor)
    record_round_scores(sidecar, this_round)
    return render_comparability(
        grader=grader_changed(previous, this_round),
        suite=suite_changed(previous, this_round),
        has_grader=True,
        has_previous=previous is not None,
    )


def record_promotion(*, sidecar: Path, round_number: int, rows: Sequence[tuple[str, str]]) -> str:
    """Append the rows a promotion was built on to the regression corpus. Append-only.

    ``rows`` are ``(row_id, reason)`` pairs, and ``promoted_in_round`` comes from ``round_number`` —
    so a caller needs no :class:`~coder_eval.models.RegressionRow` import, which is what keeps the
    skill's fence free of one.

    A later round reads this through :func:`corpus_report`: a candidate that re-loses one of these
    rows is a regression however good its aggregate looks.
    """
    if not rows:
        raise ValueError("rows is empty — a promotion with no supporting rows records nothing to check later")
    # The BEFORE count, so the block can report what actually landed. `append_regression_rows`
    # de-duplicates on `row_id`, so `len(rows)` is what was submitted, not what was added.
    before = len(load_measurements(sidecar).regression_corpus)
    appended = append_regression_rows(
        sidecar,
        [RegressionRow(row_id=row_id, promoted_in_round=round_number, reason=reason) for row_id, reason in rows],
    )
    total = len(appended.regression_corpus)
    return render_corpus_appended(submitted=len(rows), added=total - before, total=total)
