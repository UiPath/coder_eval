"""The `/coder-eval:optimize-skill` measurements sidecar — the ONE reader and writer of it.

The file lives at ``.optimize-skill/<skill>/measurements.json`` and is two things at once: a
**cache** (noise floors, replaced per key) and a **corpus** (regression rows, append-only). That
split is why the noise-floor writer replaces while the regression writer appends — re-measuring a
floor supersedes it, whereas a row an earlier promotion was built on is never rewritten. It is
deliberately NOT the free-form narrative ledger next door (``history.json``), which is append-only
prose about what happened.

Carved out of :mod:`coder_eval.optimize.gate` on the precedent
:mod:`coder_eval.leak_detection` already set: the gate DECIDES, this module PERSISTS, and a
decision layer that also owns its storage cannot be reasoned about separately from it.

**One-way dependency.** This module imports :mod:`coder_eval.models` and nothing else from the
package — never ``optimize.gate``, which imports it. ``UNRESOLVED_MODEL`` and ``UNRECORDED_SPLIT``
live here rather than with the gate for exactly that reason: both are cache-key sentinels that
``record_noise_floor`` refuses to write, so keeping them in the gate would make this module import
the gate and close a cycle.

Two stated limits of :func:`_atomic_write`, both accepted because this is a local single-agent
artifact: the read-modify-write around it is not locked, so two concurrent writers lose one set of
changes; and ``os.replace`` follows a symlink at the destination, replacing the link rather than
its target.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from uuid import uuid4

from coder_eval.models import NoiseFloor, OptimizeMeasurements, RegressionRow, RoundScores, copy_with


logger = logging.getLogger(__name__)


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
    # Both sentinels are checked here rather than at the call sites: a floor is uncacheable
    # because of what it IS, and the two reasons differ only in which key field is a placeholder.
    # The message names the field so a reader is not left guessing which one.
    uncacheable = (
        ("model", UNRESOLVED_MODEL)
        if floor.model == UNRESOLVED_MODEL
        else ("split", UNRECORDED_SPLIT)
        if floor.split == UNRECORDED_SPLIT
        else None
    )
    if uncacheable is not None:
        field, sentinel = uncacheable
        logger.info("Not caching a noise floor whose %s is %s — it could never match a lookup", field, sentinel)
        return measurements
    key = _floor_key(floor)
    kept = [f for f in measurements.noise_floors if _floor_key(f) != key]
    updated = copy_with(measurements, noise_floors=[*kept, floor])
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
        # A floor written BEFORE `split` joined the key carries no `split` in the file and
        # validates to the `None` default — which is a real, matchable value meaning "no --split
        # was passed". So a stale entry actually measured under `--split train` would answer a
        # full-suite lookup exactly, with a floor for a different row set, on the number that
        # decides whether a round runs. `model_fields_set` distinguishes the two: every floor this
        # module writes goes through `model_dump_json`, so the key is always present in a
        # current file and absent only in a pre-upgrade one. Skipping those costs one bootstrap
        # over data already on disk; serving one costs a wrong promotion decision.
        if "split" not in floor.model_fields_set:
            logger.info("Ignoring a cached noise floor written before `split` joined the key — recomputing")
            continue
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
    updated = copy_with(measurements, regression_corpus=[*measurements.regression_corpus, *fresh])
    _atomic_write(path, updated.model_dump_json(indent=2))
    return updated


def record_round_scores(path: Path, scores: RoundScores) -> OptimizeMeasurements:
    """Append one round's row vectors and Pareto front, replacing an entry for the same round.

    Replacement per round, like the noise-floor cache and unlike the regression corpus: re-running
    a round supersedes its measurement rather than adding a second, contradictory one. The vectors
    are stored whole and never truncated — being able to look back at which rows a DISCARDED
    candidate won is the entire reason they are kept rather than an average.
    """
    measurements = load_measurements(path)
    kept = [r for r in measurements.round_scores if r.round != scores.round]
    updated = copy_with(measurements, round_scores=[*kept, scores])
    _atomic_write(path, updated.model_dump_json(indent=2))
    return updated


def _fingerprint_changed(previous: str | None, current: str | None) -> bool | None:
    """The three-valued comparison both fingerprint predicates share. ``None`` means UNKNOWN.

    ``True``/``False`` require BOTH fingerprints to be present; if either is missing the answer is
    ``None``, never ``False`` — an older ``measurements.json`` predating the field, or a suite with no
    script grader, must not be able to masquerade as an instrument that provably did not change. The
    whole value of these fields is that they distinguish "the same instrument produced both numbers"
    from "nobody recorded which instrument produced them", and folding the second into the first
    would delete exactly that.

    One body rather than two identical five-line functions: the two predicates differ only in which
    field they read, and a wording or polarity fix applied to one copy is how the pair drifts.
    """
    if previous is None or current is None:
        return None
    return previous != current


def grader_changed(previous: RoundScores | None, current: RoundScores) -> bool | None:
    """Did the outcome GRADER move between these two rounds? ``None`` means UNKNOWN.

    Reported, never enforced. A changed instrument means the two rounds' scores are not comparable
    — evidence for a reader deciding whether last round's baseline still stands, not a veto.

    **Execution-track only in practice**: the activation track has no script grader, so this answers
    ``None`` on every activation round — which is correct but is NOT the sentence to print there,
    because comparability is not unknown, there is simply no script to move. That track's instrument
    provenance is :func:`suite_changed`.
    """
    if previous is None:
        return None
    return _fingerprint_changed(previous.grader_fingerprint, current.grader_fingerprint)


def suite_changed(previous: RoundScores | None, current: RoundScores) -> bool | None:
    """Did the SUITE around the grader move between these two rounds? ``None`` means UNKNOWN.

    The exact twin of :func:`grader_changed`, one field over, and three-valued for the same reason.
    Track-INDEPENDENT, unlike its sibling: the criteria, the prompt, the row set and the run caps are
    an instrument on both tracks, and on the activation track they are the WHOLE instrument. See
    :func:`coder_eval.suite_fingerprint.suite_fingerprint` for what the digest covers and what it
    deliberately does not.
    """
    if previous is None:
        return None
    return _fingerprint_changed(previous.suite_fingerprint, current.suite_fingerprint)


# Placeholder for "the caller resolved no single model" — a mixed-model or unlabelled suite. It can
# never collide with a real model id. A floor carrying it is neither read from nor written to the
# cache: borrowing another model's floor is worse than recomputing, and storing one under this key
# would accumulate entries that can never match their own lookup. PUBLIC because the skill's
# snippet needs to spell it; a literal in the prose would silently become a REAL key if this value
# ever changed.
UNRESOLVED_MODEL = "(unresolved)"

# The exact twin of UNRESOLVED_MODEL, one key field over: placeholder for "at least one of the run
# directories this floor was measured over recorded no row-selection provenance". It can never
# collide with a real split name. A floor carrying it is never WRITTEN to the cache: a floor
# measured over runs that MIGHT have used different splits is not a floor for any one of them, and
# storing it would accumulate entries that can never match their own lookup.
#
# Unlike UNRESOLVED_MODEL it is not blocked from ATTEMPTING a lookup, and does not need to be —
# nothing carrying this value is ever written, so no lookup can match. The asymmetry costs one
# scan and is stated rather than removed. PUBLIC because the gate imports it.
UNRECORDED_SPLIT = "(unrecorded)"
