"""Loading, pairing and run-tree provenance for `/coder-eval:optimize-skill`'s two gates.

Rank 0 of the optimize family: it imports nothing from its siblings and everything else imports
from it. What lives here is every question answered by READING a finalized run directory —
walking ``<run>/<variant>/<suite_id>/<row_id>/NN/task.json``, pooling replicates, pairing two arms,
reading ``run.json``'s row-selection provenance, and reconciling a tree against what its own
``run.json`` says it ran — plus the row primitives (:func:`_row_score`, :func:`_row_costs`,
:func:`_row_cost_levels`, :func:`_label_pairs`) that more than one track reduces rows with.

**Nothing here decides anything.** No promotion, no refusal, no statistic: a caller gets rows,
counts and notes, and the two gates decide on them. That is what makes the rank boundary real
rather than a filing convention.

:data:`TASK_JSON_GLOB` and :func:`_task_json_pattern` are the ONE declaration of where a row's
replicate results live and of how that path is spelled back to a user (CE042).
"""

from __future__ import annotations

import json
import logging
import statistics as _stats
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

from coder_eval.models import (
    ClassificationCriterionResult,
    EvaluationResult,
)
from coder_eval.optimize_store import UNRECORDED_SPLIT
from coder_eval.path_utils import replicate_subdir_name
from coder_eval.reports_stats import (
    mean,
)


logger = logging.getLogger(__name__)


# The one declaration of where a row's replicate results live under a suite directory — and of how
# that path is spelled back to a user. `*/*/task.json`, never a two-digit character class in the
# replicate position: that directory's NAME is owned by `path_utils.replicate_subdir_name`, and pinning its
# two-digit padding here makes BOTH gates load zero rows the day it widens, with the zero-row note
# blaming a path typo. The row id comes from `task_json.parent.parent.name`, which is
# padding-agnostic, so nothing else in the loader cares. Not shared with `reports_junit` /
# `reports_stats`: they glob one level down from a TASK dir, not two down from a SUITE dir, so a
# shared constant would be concatenated at two of three sites. CE042 is what keeps all three honest.
TASK_JSON_GLOB = "*/*/task.json"


def _task_json_pattern(variant_id: str, suite_id: str) -> str:
    """The glob as a user-facing path, so a wrong-path message cannot describe a different tree.

    Four messages tell a reader what did not match; they used to spell the pattern as a string, so
    changing the glob left three of them lying. Pass ``"<variant>"`` for a message that names both
    arms at once.
    """
    return f"<run>/{variant_id}/{suite_id}/{TASK_JSON_GLOB}"


def _wrong_path_reason(variant_id: str, suite_id: str, run_dirs: Sequence[Path]) -> str:
    """The SILENT-ZERO message: nothing matched, and that is a path fault rather than a result.

    Beside :func:`_task_json_pattern` and for the same reason. Both floors returned this sentence
    byte-identically, 130 lines apart, and the copies had already diverged: the execution twin
    appended ``or "no run dirs were given"`` for an empty sequence and the activation one did not,
    so the same fault read as ``under `` on one track and named the case on the other. The divergence
    is exactly what duplication produces, so the fuller form is folded in for both — an empty
    ``run_dirs`` now reads the same either way.

    Two paraphrases elsewhere are deliberately NOT collapsed into this: ``_load_and_pair`` prefixes
    ``the {arm} arm loaded ZERO rows:`` and ``_execution_diagnostics`` names both arms and carries a
    guardrail tail. They share this clause; they are not this message.
    """
    searched = ", ".join(str(d) for d in run_dirs) or "no run dirs were given"
    return (
        f"nothing matched {_task_json_pattern(variant_id, suite_id)} under {searched} — that "
        + "is a wrong variant id, a wrong suite id or a wrong run directory, not a measurement"
    )


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

    for task_json in sorted(suite_dir.glob(TASK_JSON_GLOB)):
        row_id = task_json.parent.parent.name
        try:
            result = EvaluationResult.model_validate_json(task_json.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load %s for the optimize gate", task_json, exc_info=True)
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


def _require_valid_criterion_index(criterion_index: int | None) -> None:
    """Reject a negative criterion index at the API boundary.

    Selection is POSITIONAL, so a negative index does not fail — it silently grades the criterion
    that many places from the END of every row's result list and returns a confident number for
    the wrong criterion. The internal guards bound only above (``criterion_index >= len(...)``),
    which is correct for the overflow case (rows legitimately differ in criteria count, so an
    over-long index skips the row) and blind to this one. ``None`` is the documented "use the row's
    weighted score" sentinel and stays legal.

    Raised, not clamped: the skill drives this module from an inline ``python`` snippet, so a wrong
    index is an authoring error that has to be loud rather than coerced into a different
    measurement. Called at the module's PUBLIC entry points only — duplicating the lower bound at
    every read site is the DRY problem this avoids, and the ``ge=0`` on the verdict models is the
    mechanical half for the persisted values.
    """
    if criterion_index is not None and criterion_index < 0:
        raise ValueError(
            f"criterion_index must be >= 0, got {criterion_index}. Selection is positional "
            + "(success_criteria[i]); a negative index would silently grade a different criterion."
        )


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


def _balance_pair[T](incumbent: list[T], candidate: list[T]) -> tuple[list[T], list[T]]:
    """Trim two arms' observations for ONE row to a common count, the shorter one winning.

    A row's weight in an arm's metric is its observation count, so an arm that contributed 3
    replicates where the other contributed 2 has silently reweighted the comparison — and the
    trigger is mundane: Stage B is three separate invocations and one interrupted run leaves a
    partial row set. Measured before this rule existed, two arms with BYTE-IDENTICAL labels on
    every row produced f1 0.818 vs 0.750 with an interval excluding zero (:func:`activation_gate`),
    and the sibling check read recall.yes 0.5 against 0.6 from one row's extra replicate.

    Generic over the element type, exactly as :func:`_floor_from_clusters` is and for the same
    reason: the guardrail trims floats, the F1 and sibling paths trim label pairs. It was spelled
    three times, in three shapes, and only one of the three surfaced the trim to the user.

    NOT used by :func:`measure_execution_noise_floor`, whose row-wise split takes a minimum ACROSS
    rows rather than between two arms of one row — a genuinely different computation that stays
    separate.
    """
    keep = min(len(incumbent), len(candidate))
    return incumbent[:keep], candidate[:keep]


def _observed_result_types(rows: dict[str, list[EvaluationResult]], criterion_index: int) -> set[str]:
    """The result types actually sitting at ``criterion_index`` — for the wrong-index note."""
    found: set[str] = set()
    for results in rows.values():
        for result in results:
            if criterion_index < len(result.success_criteria_results):
                found.add(type(result.success_criteria_results[criterion_index]).__name__)
    return found


def _row_costs(results: list[EvaluationResult]) -> list[float]:
    """Per-replicate total cost for one row, skipping replicates that recorded none."""
    return [
        result.total_token_usage.total_cost_usd
        for result in results
        if result.total_token_usage is not None and result.total_token_usage.total_cost_usd is not None
    ]


def _median(values: list[float]) -> float | None:
    """The median, or ``None`` for an empty sample — distinct from a median that happens to be 0.0."""
    return _stats.median(values) if values else None


def _row_cost_levels(clusters: Sequence[list[float]]) -> list[float]:
    """One value per row: the mean over that row's measured replicates. Empty rows are absent.

    The single definition of "what a row measured", called by :func:`cost_latency_guardrails` — for
    **both** its cost and its latency clusters — and by :func:`cost_quality_points`. Two
    implementations of it is the CE037-class defect this repo already has a lint rule for, and one
    definition is what makes the agreement test between those two surfaces writable at all. Named
    for cost because that is the reduction the two surfaces have to agree about; latency rides the
    same arithmetic.

    Takes CLUSTERS rather than the raw row mapping, because that is the shape both callers actually
    share: the guardrail reduces clusters it has already paired and balanced between two arms, and
    the N-arm view reduces one arm's clusters directly. A signature taking the row mapping could
    only serve the second, which would leave the duplication in place.
    """
    return [mean(c) for c in clusters if c]


class SplitProvenance(NamedTuple):
    """What the row-selection provenance of a set of run directories says, taken together.

    Three states, and they are NOT collapsible into two:

    - every run dir recorded the SAME value (including ``None``, i.e. "no ``--split`` was
      passed") → ``value`` is that split and the measurement is cacheable;
    - ANY run dir recorded nothing → ``value`` is ``UNRECORDED_SPLIT``: measure, but never
      cache and never match a cached entry, because a run whose provenance is missing could
      have used any row set;
    - the run dirs recorded DIFFERENT values → ``mismatched``: refuse. A null comparison
      pooled across a train and a test invocation is not a floor, and a gate pairing them is
      comparing two row sets and reporting the difference as one measurement.

    A run.json that recorded a selection whose ``split`` is ``null`` is **recorded**, not
    unrecorded — the first says no split was passed, the second says nothing at all.
    """

    recorded: frozenset[str | None]
    unrecorded: int

    @property
    def mismatched(self) -> bool:
        return len(self.recorded) > 1

    @property
    def value(self) -> str | None:
        """The single recorded split, or ``UNRECORDED_SPLIT`` when any dir carried none.

        Only meaningful when not :attr:`mismatched` — a mismatch is refused before this is read.
        """
        if self.unrecorded or not self.recorded:
            # `not self.recorded` covers an EMPTY run_dirs sequence. Returning `None` there would
            # be indistinguishable from a genuinely recorded full-suite run and would be stamped
            # onto a NoiseFloor as one. Unreachable through today's callers (both floor functions
            # guard on an empty set earlier), but the type should be right on its own rather than
            # by an artefact of call ordering.
            return UNRECORDED_SPLIT
        return next(iter(self.recorded))


def read_split_provenance(run_dirs: Sequence[Path]) -> SplitProvenance:
    """Read ``row_selection.split`` from each run root's ``run.json``.

    A missing, unreadable or malformed ``run.json``, an absent ``row_selection``, or a
    ``row_selection`` of ``null`` all count as **unrecorded** — never as a recorded ``None``.
    That distinction is the whole point: "this run did not use ``--split``" and "we cannot
    tell what this run used" support very different conclusions, and only the first is
    comparable against another run.

    Catches ``OSError`` (unreadable file) and ``ValueError`` (a JSON decode error is one);
    a run directory that predates the provenance field is an ordinary, expected input here,
    not an error worth aborting a gate over.
    """
    recorded: set[str | None] = set()
    unrecorded = 0
    for run_dir in run_dirs:
        try:
            payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unrecorded += 1
            continue
        selection = payload.get("row_selection") if isinstance(payload, dict) else None
        if not isinstance(selection, dict):
            unrecorded += 1
            continue
        split = selection.get("split")
        # The LEAF is validated too, not just its container. run.json is untrusted here — it may be
        # hand-edited, or pulled from blob storage, or written by a newer coder-eval — and an
        # unhashable value (a dict) raised `TypeError` straight out of a function whose entire
        # contract is to degrade, while a non-string scalar was accepted into a
        # `frozenset[str | None]` and then crashed the SORT that builds the refusal message.
        # Both are "we cannot tell what this run selected", which is exactly `unrecorded`.
        if split is not None and not isinstance(split, str):
            unrecorded += 1
            continue
        recorded.add(split)
    return SplitProvenance(recorded=frozenset(recorded), unrecorded=unrecorded)


class TreeReconciliation(NamedTuple):
    """What one run dir's tree holds for one arm, against what its ``run.json`` says it ran.

    ``unrecorded`` is the ``(row_id, replicate_dir)`` pairs present on disk that ``run.json`` does
    not describe. ``unknown`` is set when the question could not be asked at all — no ``run.json``,
    unreadable, or no ``task_results`` — which is a NOTE, never a refusal.
    """

    unrecorded: frozenset[tuple[str, str]]
    recorded: int
    on_disk: int
    unknown: bool


# The replicate level of the suite glob, DERIVED rather than respelled: `TASK_JSON_GLOB` is
# `<row>/<NN>/task.json` relative to a suite dir, so dropping its first segment gives the same
# pattern relative to one row dir. Spelling it again would let the two describe different trees,
# which is the whole reason `_task_json_pattern` exists (CE042).
_REPLICATE_TASK_JSON_GLOB = TASK_JSON_GLOB.split("/", 1)[1]


def reconcile_tree_against_run_json(run_dir: Path, variant_id: str, suite_id: str) -> TreeReconciliation:
    """Does this ``run.json`` describe the arm's replicates sitting in this tree?

    ``run.json`` is a per-INVOCATION artifact; the tree under it is APPEND-ONLY. Nothing removes a
    prior invocation's results, so a reused ``--run-dir`` leaves stale ``<row>/<NN>/task.json``
    behind while ``row_selection`` is rewritten to describe only the latest call. The recorded
    provenance is then a true statement about an invocation and a FALSE one about the tree the
    gate globs — and because both arms are subdirectories of the same run dir, the contamination
    is SYMMETRIC: the stale results pair on both sides, so there is no ``rows_excluded`` bump and
    no unpaired-rows note. The only trace is a ``rows_paired`` larger than the selected split, or
    a replicate count larger than the ``--repeats`` asked for, and nothing else flags either.

    Matched by ``(row id, replicate dir)``, not by counting. A fanned row's ``task_id`` is
    ``f"{suite_id}/{row_id}"`` (``task_loader.expand_dataset``) and ``path_utils.build_task_run_dir``
    uses that same string as the path segment, so the tree's directory name and ``run.json``'s
    ``task_id`` correspond by CONSTRUCTION rather than by coincidence. Counts would be blind to a
    reused dir whose two invocations happened to run the same number of rows, and would need a
    fixed glob depth the tree does not have — a dataset row sits at
    ``<variant>/<suite>/<row>/<NN>/`` while a plain task sits at ``<variant>/<task>/<NN>/``, and
    one run dir can hold both.

    **The REPLICATE half of the key is load-bearing, not thoroughness.** Row ids alone are blind to
    a stale ``<NN>`` inside a row this run.json does record — the identical defect one level down,
    triggered by something as mundane as re-using a run dir with a smaller ``--repeats``.
    ``load_suite_rows`` pools every replicate it finds, ``_balance_pair`` trims symmetrically and
    therefore not at all, and the gate returns a confident interval over contaminated clusters.
    ``task_results`` carries ``replicate_index``, so this costs nothing.

    Reads directory NAMES only — no ``task.json`` is parsed — so the tree half costs a listing.

    An entry whose ``variant_id`` is absent counts for EVERY variant, and one whose
    ``replicate_index`` is absent counts for every replicate of its row. Both ambiguities are
    resolved permissively on purpose: the harm here is a false refusal blocking a legitimate
    promotion, not a missed detection, and an unattributable entry means "we cannot rule this one
    in", not "this one is stale".

    **What this cannot see, stated here rather than left to be discovered.** A ``run.json`` rebuilt
    by ``coder-eval aggregate`` is rebuilt FROM the tree (``recover_task_results`` walks it), so it
    describes everything on disk by construction and genuine contamination is laundered into a
    clean reading. That is accepted: this is strictly better than the nothing it replaces, not a
    proof. ``--resume`` is a different path and is NOT affected — ``partition_for_resume`` folds
    ``prior_results`` for the resolved task set into the new summary, so a resume under the SAME
    selector still describes its whole tree, and a resume under a DIFFERENT one genuinely has
    contaminated the tree and is refused correctly. Nested sub-run dirs (a subdirectory carrying
    its own ``run.json``) are not excluded here, unlike ``recover_task_results`` — ``load_suite_rows``
    is blind to them the same way, so the gate already conflates them and this adds no new gap.
    """
    suite_dir = run_dir / variant_id / suite_id
    on_disk: set[tuple[str, str]] = set()
    try:
        row_dirs = [d for d in suite_dir.iterdir() if d.is_dir()] if suite_dir.is_dir() else []
        for row_dir in row_dirs:
            on_disk |= {(row_dir.name, task_json.parent.name) for task_json in row_dir.glob(_REPLICATE_TASK_JSON_GLOB)}
    except OSError:
        # An unreadable directory is "we cannot tell", exactly like an unreadable run.json. The
        # contract of this function is to degrade, and `read_split_provenance` catches the same
        # class of fault beside it.
        return TreeReconciliation(frozenset(), 0, 0, unknown=True)

    try:
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TreeReconciliation(frozenset(), 0, len(on_disk), unknown=True)
    results = payload.get("task_results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return TreeReconciliation(frozenset(), 0, len(on_disk), unknown=True)

    prefix = f"{suite_id}/"
    recorded: set[tuple[str, str]] = set()
    whole_rows: set[str] = set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        entry_variant = entry.get("variant_id")
        if entry_variant is not None and entry_variant != variant_id:
            continue
        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or not task_id.startswith(prefix):
            continue
        row_id = task_id[len(prefix) :]
        replicate = entry.get("replicate_index")
        if isinstance(replicate, int):
            recorded.add((row_id, replicate_subdir_name(replicate)))
        else:
            whole_rows.add(row_id)
    unrecorded = frozenset(pair for pair in on_disk if pair not in recorded and pair[0] not in whole_rows)
    return TreeReconciliation(unrecorded, len(recorded) + len(whole_rows), len(on_disk), unknown=False)


def _reconcile_arms(
    arms: Sequence[tuple[str, Sequence[Path]]], suite_id: str
) -> tuple[dict[str, frozenset[tuple[str, str]]], int]:
    """Reconcile every ``(variant, run dirs)`` arm: stale results keyed by location, plus unknowns.

    The WHOLE-ARM sweep, and the one declaration of it: :func:`activation_gate`'s preflight, both
    noise floors and :func:`arm_row_scores` all route through it. :func:`execution_gate` is the one
    reader that does NOT and cannot — it works one run dir per variant and needs each dir's own
    :class:`TreeReconciliation` to name the single location its richer refusal quotes, so it calls
    :func:`reconcile_tree_against_run_json` directly. CE053 therefore accepts either name.

    What is deliberately NOT shared is the RESPONSE, which differs by return type: a gate refuses,
    a floor returns ``None`` through :func:`_no_floor`, and ``ArmRowScores`` has nowhere to put a
    refusal so it warns and continues. One helper spanning all three would take a mode flag, which
    is two functions in a trench coat.

    ``unknown`` is counted rather than collected: a run dir whose ``run.json`` is missing,
    unreadable or predates ``task_results`` cannot be reconciled either way, which is always a NOTE
    and never a refusal — old run dirs stay gatable. **The debug line for it is emitted HERE**, once,
    because it says the same thing for every caller and was a verbatim six-line copy in the two
    floors. A caller that owes the USER a note rather than a log builds its own — only
    ``activation_gate`` does, since only its return type has a ``notes`` channel to put one in.
    """
    stale: dict[str, frozenset[tuple[str, str]]] = {}
    unknown_dirs = 0
    total_dirs = 0
    for variant, dirs in arms:
        for run_dir in dirs:
            total_dirs += 1
            reconciliation = reconcile_tree_against_run_json(run_dir, variant, suite_id)
            if reconciliation.unknown:
                unknown_dirs += 1
            elif reconciliation.unrecorded:
                stale[f"{run_dir}/{variant}"] = reconciliation.unrecorded
    if unknown_dirs:
        # `debug`, not `warning`: a floor that WAS measured must not log through the channel
        # `_no_floor` uses to say it was not.
        logger.debug(
            "%d of %d run directories for %s record no `task_results`, so contamination could not be ruled out",
            unknown_dirs,
            total_dirs,
            ", ".join(repr(variant) for variant, _dirs in arms),
        )
    return stale, unknown_dirs


def _stale_locations(stale: dict[str, frozenset[tuple[str, str]]]) -> str:
    """The stale results PER LOCATION — never a tree-wide total.

    A sum over (arm x dir) is unreconcilable with the ``Rows paired: N`` line in the same block —
    measured, a 22-row suite over 3 dirs and 2 arms reported "124 of 130" — and the number the
    reader must act on is how many results in WHICH directory nothing wrote.
    """
    return "; ".join(
        f"{location}: {len(pairs)} result(s) across {len({row for row, _ in pairs})} row(s)"
        + f" (e.g. {', '.join(f'{row}/{rep}' for row, rep in sorted(pairs)[:3])}"
        + f"{', …' if len(pairs) > 3 else ''})"
        for location, pairs in sorted(stale.items())
    )


def _stale_tree_reason(stale: dict[str, frozenset[tuple[str, str]]]) -> str:
    """Why a tree holding unrecorded results is not something to measure over.

    Shared by both noise floors (which REFUSE with it, through :func:`_no_floor`) and by
    :func:`arm_row_scores` (which WARNS with it and continues). Sharing the message is what makes
    the two responses read alike in a log; the responses themselves stay separate — see
    :func:`_reconcile_arms`.

    Deliberately NOT shared with the two gates' refusals, which say more: ``activation_gate``'s
    names both arms and appends an unreconcilable-directory tail, ``execution_gate``'s names the
    one variant it broke on. Those are decisions a user acts on; this is a measurement declining.
    """
    return (
        f"the run directory tree holds results that no recorded invocation wrote — {_stale_locations(stale)}. "
        + "run.json is written per INVOCATION while the tree is APPEND-ONLY, so a re-used --run-dir leaves an "
        + "earlier call's results behind while `row_selection` is rewritten to describe only the latest one. "
        + "They are pooled into this measurement, which is therefore over a row set no invocation ran. Re-run "
        + "into a fresh --run-dir."
    )


def _format_splits(values: Iterable[str | None]) -> str:
    """The recorded splits as one readable list, ``None`` first.

    Shared by the floor's refusal and the gate's, because the whole value of those two messages is
    that a reader recognises the same vocabulary in both. It is also the single place the sort key
    lives — `read_split_provenance` guarantees every element is `str | None`, and this is what
    would break first if that ever stopped being true.
    """
    return ", ".join(repr(v) for v in sorted(values, key=lambda v: (v is not None, v or "")))


def _split_mismatch_reason(label: str, provenance: SplitProvenance, run_dirs: Sequence[Path]) -> str:
    """The message a cross-split refusal carries, naming the splits AND where they came from."""
    where = ", ".join(str(d) for d in run_dirs)
    return (
        f"{label} pooled run directories recording DIFFERENT row selections "
        f"(splits: {_format_splits(provenance.recorded)}) under {where}"
    )


class _PairedRows(NamedTuple):
    """Everything :func:`activation_gate` needs from the two arms' run directories, already paired.

    Six concerns used to be interleaved in the gate's first hundred lines: loading both arms,
    pairing and reporting the unpaired, the zero-row wrong-path note, hollow-row exclusion,
    replicate balancing, and the wrong-index note. None of them touches a statistic, and every one
    of them appends to ``notes`` — which is why they read as one step and extract as one.

    The clusters and the flattened pairs are carried rather than the balanced mapping they come
    from: the gate's remaining body wants exactly these four (two for the bootstrap, two for the
    reported F1s) and nothing else, so returning the mapping would leave the caller re-deriving
    them — a second spelling of the flattening this helper just did.
    """

    incumbent_by_dir: list[dict[str, list[EvaluationResult]]]
    candidate_by_dir: list[dict[str, list[EvaluationResult]]]
    incumbent_rows: dict[str, list[EvaluationResult]]
    candidate_rows: dict[str, list[EvaluationResult]]
    scored_row_ids: list[str]
    incumbent_clusters: list[list[tuple[str, str]]]
    candidate_clusters: list[list[tuple[str, str]]]
    incumbent_pairs: list[tuple[str, str]]
    candidate_pairs: list[tuple[str, str]]
    rows_excluded: int
    n_discordant: int
    notes: list[str]


def _load_and_pair(
    *,
    incumbent_run_dirs: Sequence[Path],
    candidate_run_dirs: Sequence[Path],
    incumbent_variant: str,
    candidate_variant: str,
    suite_id: str,
    criterion_index: int,
) -> _PairedRows:
    """Load both arms, pair their rows, and report everything that made the sample smaller.

    Every note this produces is about the SAMPLE — a wrong path, an asymmetric row set, a row that
    errored, an unbalanced replicate count, a wrong criterion index. None of them is about a
    statistic, and the gate cannot compute one until all of them have been applied.

    The returned ``notes`` list is the caller's to keep appending to. It is returned rather than
    copied because pydantic COPIES the list at construction, so a note appended after the model is
    built is silently discarded — the gate must therefore hold this exact list until its return.
    """
    # CE053: this reader does NOT reconcile, and its only caller is why. `activation_gate` runs the
    # sweep itself, over both arms at once, and REFUSES before reading a statistic — reconciling
    # here as well would read every run.json twice per arm for one fault.
    incumbent_by_dir = [load_suite_rows(d, incumbent_variant, suite_id) for d in incumbent_run_dirs]  # noqa: CE053
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
                + f"{_task_json_pattern(variant_id, suite_id)} under {searched}. "
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

    # BALANCE the replicate counts per row before pooling. A row's weight in an arm's f1.yes is
    # its number of observations, so an arm that contributed 3 replicates for a row while the other
    # contributed 2 has silently reweighted the comparison — and the trigger is mundane: Stage B is
    # three separate invocations, and one interrupted run leaves a partial row set. Measured, two
    # arms with BYTE-IDENTICAL labels on every row produced f1 0.818 vs 0.750 with an interval
    # excluding zero and rows_excluded == 0. Truncating each row to min(n_incumbent, n_candidate)
    # makes every row weigh the same on both sides; the dropped observations are counted and noted.
    balanced: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
    dropped = 0
    for rid in scored_row_ids:
        inc, cand = per_row[rid]
        kept_inc, kept_cand = _balance_pair(inc, cand)
        dropped += len(inc) + len(cand) - len(kept_inc) - len(kept_cand)
        balanced[rid] = (kept_inc, kept_cand)
    unbalanced_rows = [rid for rid in scored_row_ids if len(per_row[rid][0]) != len(per_row[rid][1])]
    if unbalanced_rows:
        notes.append(
            f"{len(unbalanced_rows)} row(s) had different replicate counts on the two arms and were "
            + f"trimmed to the smaller count, dropping {dropped} observation(s): "
            + f"{', '.join(unbalanced_rows)}. A row's weight in an arm's F1 is its observation count, "
            + "so an unbalanced row shifts the comparison on its own — usually an interrupted "
            + "invocation. Re-run it rather than reading the interval below as an effect."
        )

    incumbent_clusters = [balanced[rid][0] for rid in scored_row_ids]
    candidate_clusters = [balanced[rid][1] for rid in scored_row_ids]

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

    # A row is DISCORDANT when the arms' pooled pair multisets differ — `sorted`, not `==`, so a
    # row whose two arms carry the same pairs in a different replicate order counts as concordant.
    # Only discordant rows can move a resample's difference off exactly 0.0, which is what makes
    # `_discreteness_floor` a valid bound on the smallest p this suite can be expected to express.
    n_discordant = sum(1 for rid in scored_row_ids if sorted(balanced[rid][0]) != sorted(balanced[rid][1]))

    return _PairedRows(
        incumbent_by_dir=incumbent_by_dir,
        candidate_by_dir=candidate_by_dir,
        incumbent_rows=incumbent_rows,
        candidate_rows=candidate_rows,
        scored_row_ids=scored_row_ids,
        incumbent_clusters=incumbent_clusters,
        candidate_clusters=candidate_clusters,
        incumbent_pairs=incumbent_pairs,
        candidate_pairs=candidate_pairs,
        # Computed once here rather than at each of the gate's two returns, which is where it used
        # to be spelled twice.
        rows_excluded=len(unpaired) + unscored_count,
        n_discordant=n_discordant,
        notes=notes,
    )


def _row_score(result: EvaluationResult, criterion_index: int | None) -> float | None:
    """The row's score for one arm: the criterion's score, or the row's weighted score.

    ``None`` when the row produced no criterion results at all. That case matters on the execution
    track specifically: ``calculate_weighted_score`` sets ``weighted_score`` to 0.0 for an empty
    result list, so an errored or timed-out row arrives as a *scored zero* rather than a hole — and
    the arm is then discarded from the Pareto front for having crashed, with no `—` in the matrix
    to show it. A hole is not a failure, and this is where that distinction is enforced.
    """
    if not result.success_criteria_results:
        return None
    if criterion_index is None:
        return result.weighted_score
    if criterion_index >= len(result.success_criteria_results):
        return None
    return result.success_criteria_results[criterion_index].score


# The grader's machine-readable attribution line, and the ONE declaration of its prefix. The
# contract is `plugins/coder-eval/reference/templates/outcome-grader/verify.py`'s: the LAST line of
# the grader's stdout is `RULES ` followed by compact JSON of rule id -> "pass" | "fail" | "na".
RULES_LINE_PREFIX = "RULES "


def _rules_verdicts(result: EvaluationResult, criterion_index: int) -> dict[str, str] | None:
    """One replicate's rule attribution, or ``None`` when it carried none.

    Scans the criterion's ``details`` from the END for :data:`RULES_LINE_PREFIX`, **within the
    stdout section only**. ``run_command`` wraps the grader's stdout in a details block and appends
    a ``Stderr:`` section after it, so the grader's own last line is never the details' last line —
    and a naive reverse scan reads the STDERR side first. That matters beyond tidiness: a
    traceback there can quote artifact text, which is untrusted agent output, so a forged
    ``RULES {...}`` line would outrank the grader's real one.

    The window is ``[first "Stdout:" ... first "Stderr:" after it)``, and **both must be the
    FIRST** — an earlier draft ended it at the LAST ``Stderr:``, which the same untrusted text
    defeats simply by containing a second such line, moving the boundary back past the grader's
    real one. Everything before the wrapper's ``Stdout:`` is written by the criterion (``Score:``,
    ``Command:``, ``Exit code:``), so the first marker is always its own.

    With no ``Stdout:`` marker — a criterion reporting raw stdout — the whole field is scanned. A
    grader printing its own ``Stderr:`` line truncates the window early and its row reads as
    unattributed, which fails CLOSED: a missing attribution is warned about, a forged one is not.

    ``None`` — no line, unparseable JSON, or a payload that is not an object — is deliberately not
    distinguished from ``{}`` at this level. ``{}`` means a CURRENT grader that attributed nothing;
    ``None`` means no attribution is available for this replicate at all, which is what
    :func:`rule_row_map` counts.

    **What it cannot see**, stated rather than left to be discovered: ``run_command`` truncates each
    stream at 4000 characters, so a grader verbose enough to overflow that budget loses this line
    and its row reads as unattributed.
    """
    if criterion_index >= len(result.success_criteria_results):
        return None
    details = result.success_criteria_results[criterion_index].details
    if not details:
        return None
    lines = details.splitlines()
    stdout_at = next((i for i, line in enumerate(lines) if line.startswith("Stdout:")), None)
    if stdout_at is not None:
        after = lines[stdout_at + 1 :]
        stderr_at = next((i for i, line in enumerate(after) if line.startswith("Stderr:")), len(after))
        lines = after[:stderr_at]
    for line in reversed(lines):
        if not line.startswith(RULES_LINE_PREFIX):
            continue
        try:
            parsed = json.loads(line[len(RULES_LINE_PREFIX) :])
        except ValueError:
            logger.warning("A grader emitted an unparseable %s line: %r", RULES_LINE_PREFIX.strip(), line)
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class RuleAttribution(NamedTuple):
    """What the graders said about rules, and which rows said nothing at all.

    ``unattributed`` is RETURNED rather than only logged, because a consumer cannot recompute it
    and the ceiling is wrong without it: a row carrying no ``RULES`` line is in no rule's failing
    set, so every ceiling is an UNDER-estimate — and the ``GAP`` verdict, the one that says "stop
    working on this rule", can then be produced by a truncated log rather than by a suite with no
    headroom. ``run_command`` truncates each stream at 4000 characters, so that is a real path.
    """

    failed: dict[str, set[str]]  # every rule SEEN -> the rows it failed on, possibly empty
    unattributed: list[str]  # rows whose replicates carried no RULES line at all


def rule_row_map(rows: dict[str, list[EvaluationResult]], criterion_index: int) -> RuleAttribution:
    """Rule id -> the row ids where that rule FAILED, read off the grader's ``RULES`` lines.

    The missing link between an outcome suite's grader and the headroom ceiling: without it,
    ``headroom_ceiling``'s row subsets would have to be typed by hand from the detail lines.
    ``criterion_index`` is the POSITION of the grader's ``run_command`` criterion in the suite's
    ``success_criteria`` — the same positional convention every other reader here uses, and the
    reason a negative index is rejected rather than silently wrapping.

    **Any-replicate failure marks the row**, matching the grader's own any-check rule for one row.
    Both point the same way on purpose: each counts the MOST rows as failing, so the ceiling
    computed from them is an UPPER bound, and "this rule cannot clear the floor" is a claim that
    survives the most generous attribution available.

    **Every rule SEEN is a key**, including one that only ever passed or was N/A, mapping to an
    empty set. Two things follow, and both were bugs in the version that keyed only failures: the
    rule everything already passes can be SIZED (its ceiling is 0.0 — "this suite cannot show an
    improvement to it", a real finding rather than a rule missing from the table), and
    ``rule_map.failed[rule]`` is never a missing key, which removes the ``rows=None``-versus-
    ``rows=set()`` trap from every consumer.

    An empty ``failed`` means attribution is unavailable (a pre-contract grader, or a criterion
    index pointing at something else); the caller's remedy is the suite-level ceiling.

    Takes already-loaded rows, so CE053 does not apply: whoever loaded them reconciled them.
    """
    _require_valid_criterion_index(criterion_index)
    failed: dict[str, set[str]] = {}
    unattributed: list[str] = []
    for row_id, results in sorted(rows.items()):
        verdicts = [v for r in results if (v := _rules_verdicts(r, criterion_index)) is not None]
        if not verdicts:
            unattributed.append(row_id)
            continue
        for verdict in verdicts:
            for rule, outcome in verdict.items():
                # `setdefault` on EVERY verdict, not only a failing one: a rule this suite always
                # passes has a real ceiling (zero), and a caller that never sees the key cannot ask.
                seen = failed.setdefault(rule, set())
                if outcome == "fail":
                    seen.add(row_id)
    if unattributed:
        logger.warning(
            "%d of %d row(s) carry no %s line at criterion_index=%d and are attributed to no rule: %s",
            len(unattributed),
            len(rows),
            RULES_LINE_PREFIX.strip(),
            criterion_index,
            ", ".join(unattributed),
        )
    return RuleAttribution(failed=failed, unattributed=unattributed)
