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
from coder_eval.optimize.activation import noise_floor_mde
from coder_eval.optimize.execution import measure_execution_noise_floor, resolve_arm_model
from coder_eval.optimize.store import UNRESOLVED_MODEL, load_measurements
from coder_eval.reports_optimize import render_noise_floor


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
