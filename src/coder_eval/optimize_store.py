"""The `/coder-eval:optimize-skill` measurements sidecar — the ONE reader and writer of it.

The file lives at ``.optimize-skill/<skill>/measurements.json`` and is two things at once: a
**cache** (noise floors, replaced per key) and a **corpus** (regression rows, append-only). That
split is why the noise-floor writer replaces while the regression writer appends — re-measuring a
floor supersedes it, whereas a row an earlier promotion was built on is never rewritten. It is
deliberately NOT the free-form narrative ledger next door (``history.json``), which is append-only
prose about what happened.

Carved out of :mod:`coder_eval.optimize_gate` on the precedent
:mod:`coder_eval.leak_detection` already set: the gate DECIDES, this module PERSISTS, and a
decision layer that also owns its storage cannot be reasoned about separately from it.

**One-way dependency.** This module imports :mod:`coder_eval.models` and nothing else from the
package — never ``optimize_gate``, which imports it. ``UNRESOLVED_MODEL`` lives here rather than
with the gate for exactly that reason: it is a cache-key sentinel (``record_noise_floor`` refuses
to write it, ``lookup_noise_floor`` can never match it), so keeping it in the gate would make this
module import the gate and close a cycle.

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

from coder_eval.models import NoiseFloor, OptimizeMeasurements, RegressionRow, RoundScores


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


# Placeholder for "the caller resolved no single model" — a mixed-model or unlabelled suite. It can
# never collide with a real model id. A floor carrying it is neither read from nor written to the
# cache: borrowing another model's floor is worse than recomputing, and storing one under this key
# would accumulate entries that can never match their own lookup. PUBLIC because the skill's
# snippet needs to spell it; a literal in the prose would silently become a REAL key if this value
# ever changed.
UNRESOLVED_MODEL = "(unresolved)"
