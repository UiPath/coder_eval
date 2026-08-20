"""The search loop's accept/revert decision, and the anti-memorization preflight.

Rank 3 of the optimize family, beside :mod:`coder_eval.optimize.fronts` and importing nothing from
it — :class:`~coder_eval.models.ArmRowScores` is a MODEL, so there is no edge between them.

**Nothing here is a gate.** :func:`search_compare` compares across invocations, unpaired,
unreplicated and uncorrected — the arithmetic the promotion gate exists to distrust — so a ``True``
is a hypothesis to gate and nothing here promotes. It lives in code rather than in the skill's
prose because each of its guards is silent when omitted, and the previous home for them was a
markdown block an agent copies and adapts.

:func:`candidate_leaks` is the anti-memorization preflight: static, needing no runs, so the skill
reads it at proposal time before Stage A is paid for. It stays PURE — two strings and a row list —
while :func:`skill_text` beside it does the IO, reading a whole skill DIRECTORY. That split is the
point: a candidate that bundles train-row content into ``scripts/`` or a reference file is invisible
to a one-file scan and comes back clean, so the scan has to read a tree; and widening it inside the
checker would have made a pure function reach the filesystem.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from coder_eval.leak_detection import graded_strings
from coder_eval.models import (
    ArmRowScores,
    OptimizeMeasurements,
    RegressionRow,
    TaskDefinition,
)
from coder_eval.reports_stats import (
    mean,
)


logger = logging.getLogger(__name__)


def regression_check(
    corpus: list[RegressionRow], arm: ArmRowScores, *, threshold: float = 1.0
) -> list[tuple[RegressionRow, float | None]]:
    """Corpus rows this arm did not fully score — the promotions it would quietly undo.

    The corpus is written on every promotion (:func:`append_regression_rows`) and, until this
    function existed, read by nothing but that writer's own de-duplication. This is the read: a
    candidate that re-loses a row an earlier promotion was built on is a regression however good
    its aggregate looks, and an aggregate cannot show it.

    One entry per corpus row that did not clear ``threshold``, in corpus order:

    - ``(row, score)`` when the arm scored it below the bar — a measured loss.
    - ``(row, None)`` when the arm has no score for it at all. **A hole is reported, never
      skipped**, the same rule :func:`_dominates` applies to the row vector: not measuring a row is
      not passing it. The two causes are indistinguishable from the corpus alone — the row errored
      in this run, or it belongs to the skill's OTHER suite, since the corpus is per skill and a
      skill may have both an activation and an outcome suite. Check which before reporting it.

    Rows at or above ``threshold`` are absent, so an empty result is the clean answer.

    ``threshold`` defaults to 1.0, which treats any partial score as a loss. That is right for the
    binary activation criterion the corpus is usually written from; the parameter exists for a
    fractional execution suite. Note that ``arm.row_scores`` values are means over replicates, so a
    row that passed 2 of 3 replicates reads 0.667 and is reported at the default — correctly, since
    a row that became flaky is a row the promotion no longer holds on.
    """
    findings: list[tuple[RegressionRow, float | None]] = []
    for row in corpus:
        score = arm.row_scores.get(row.row_id)
        if score is None or score < threshold:
            findings.append((row, score))
    return findings


def skill_text(skill_dir: Path) -> str:
    """Every text file under a skill directory, concatenated in sorted relative-path order.

    What :func:`candidate_leaks` should be handed. Scanning ``SKILL.md`` alone leaves a candidate
    that bundles train-row content into ``scripts/`` or a reference file completely invisible, and
    the result comes back **clean** — byte-identical to a genuinely clean candidate.

    Each file's content is preceded by its relative POSIX path, for two reasons: a span's location
    stays recoverable from the returned text, and two files cannot be concatenated into a phantom
    match across the boundary between them. ``as_posix()`` so a Windows checkout produces the same
    string.

    **Skips what it cannot read and what it must not reach, and nothing else.** A file that does not
    decode as UTF-8 — an image in a reference directory — is skipped, because a binary cannot carry a
    verbatim graded string in the form :func:`~coder_eval.leak_detection.graded_strings` produces, and
    so is one that raises ``OSError`` (a permission, or a file that vanished mid-walk): a preflight
    must not abort a round over an unreadable stray file. Symlinks are not followed in either form —
    a linked FILE is skipped by the explicit check below, and a linked DIRECTORY is never descended
    into because ``rglob`` does not recurse through one. Either would otherwise scan arbitrary files
    on the machine.

    The cost is O(text) once per arm, before Stage A is paid for, so a skill with a large reference
    corpus is slower to preflight and costs no runs.
    """
    parts: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        # `is_symlink` FIRST: `is_file` follows the link, so a symlink to a real file passes it.
        if path.is_symlink() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        parts.append(f"{path.relative_to(skill_dir).as_posix()}\n{content}")
    return "\n".join(parts)


def candidate_leaks(
    candidate_text: str,
    baseline_text: str,
    rows: Sequence[TaskDefinition],
) -> list[str]:
    """Train-row content a candidate skill newly contains. The anti-memorization preflight.

    Returns one entry per leak: the row it came from, the field, and the offending span. Empty means
    nothing was found — which is not the same as nothing being there, see the boundary below.

    **It compares TEXT, and the caller owns what that text covers.** Hand it
    :func:`skill_text(skill_dir) <skill_text>`, never one file's contents: a skill is a DIRECTORY,
    and a candidate that bundles train-row content into ``scripts/`` or a reference file is then
    invisible — the result comes back CLEAN, byte-identical to a genuinely clean candidate, which is
    the worst shape a preflight can have.

    ``baseline_text`` is what makes it NEWLY: content the incumbent already had is not a leak the
    candidate introduced.

    Shares its primitive with CE036 rather than reimplementing it: ``LEAK_LOCATOR_FIELDS``,
    ``LEAK_MIN_CHARS``, ``string_leaves`` and ``graded_strings`` live in
    :mod:`coder_eval.leak_detection`. The one behavioural difference is a parameter, not a fork —
    ``graded_strings(drop_type=True)`` here, because a skill BODY discussing eval criteria names
    types legitimately, while a row PROMPT saying "skill_triggered" is worth flagging.

    **Boundary:** verbatim spans of at least ``LEAK_MIN_CHARS``. A candidate that PARAPHRASES a train
    row is invisible to this, and so is one that encodes an answer structurally. It bounds the crudest
    failure and is not a proof of generalization.
    See .claude/decisions/2026-08-20-anti-memorization-and-search.md.
    """
    candidate = candidate_text.lower()
    baseline = baseline_text.lower()
    findings: list[str] = []
    for row in rows:
        for criterion in row.success_criteria:
            for value in graded_strings(criterion, drop_type=True):
                lowered = value.lower()
                if lowered in candidate and lowered not in baseline:
                    findings.append(f"{row.task_id}: candidate adds {value!r} ({criterion.type})")
    return list(dict.fromkeys(findings))


class SearchComparison(NamedTuple):
    """The search loop's accept/revert decision for one round's single candidate.

    A NamedTuple beside :class:`CostQualityPoint`, and for the same reason: computed and rendered,
    never persisted. What IS persisted is the outcome — ``RoundScores.lineage_head`` — and that is
    a model.

    ``beats`` and ``blocker`` are deliberately two fields rather than one. ``beats`` is the score
    comparison alone; ``blocker`` is what stands in its way. Collapsing them would make a corpus
    regression indistinguishable from a candidate that simply scored worse, and those two call for
    opposite next actions — one is "look at the row and decide", the other is "write the next
    hypothesis". ``accepted`` is their conjunction and is DERIVED (see below), never stored.
    """

    beats: bool
    head_score: float | None
    candidate_score: float | None
    shared_rows: tuple[str, ...]
    holes: tuple[str, ...]
    regressions: tuple[tuple[RegressionRow, float | None], ...]
    blocker: str | None

    @property
    def accepted(self) -> bool:
        """``beats`` AND nothing blocking it — derived, never set.

        It was a field, and every construction site computed exactly this expression, so the two
        could be set inconsistently by a caller and nothing would notice. ``beats`` and ``blocker``
        stay separate fields because a corpus block and a plain loss call for opposite next
        actions — "look at the row and decide" against "write the next hypothesis" — and that
        distinction lives in those two, not in their conjunction.
        """
        return self.beats and self.blocker is None


def lineage_head_scores(measurements: OptimizeMeasurements) -> ArmRowScores | None:
    """The arm the most recent round carried forward, or ``None`` when no round named one.

    The highest ``round`` that recorded a ``lineage_head``, **not** the last entry in the list:
    ``record_round_scores`` replaces per round, so list order is a write-order artefact while
    ``round`` is the real sequence. A later round that accepted nothing is skipped rather than
    blanking the lineage — a quiet round leaves the head where it was.

    ``RoundScores``' own validator guarantees the named arm is present with a non-empty vector, so
    the lookup below cannot raise.
    """
    named = [r for r in measurements.round_scores if r.lineage_head is not None]
    if not named:
        return None
    last = max(named, key=lambda r: r.round)
    return next(a for a in last.arm_row_scores if a.variant_id == last.lineage_head)


def search_compare(
    head: ArmRowScores,
    candidate: ArmRowScores,
    *,
    corpus: Sequence[RegressionRow] = (),
    threshold: float = 1.0,
) -> SearchComparison:
    """Accept or revert ONE search step. Emphatically NOT a gate.

    Returns a :class:`SearchComparison` whose ``accepted`` is a property over ``beats`` and
    ``blocker``, so no construction site can set it inconsistently with the numbers it derives from.

    **It does not correct for multiplicity and must never be read as a promotion.** It compares one
    arm at a time inside a search, where the alternative is reverting a step rather than shipping a
    skill. The gates are :func:`~coder_eval.optimize.activation.activation_gate` and
    :func:`~coder_eval.optimize.execution.execution_gate`, and both go through Holm.

    A hole is ABSENT, never zero: an arm with no measurement on a row is missing there, and folding
    it to 0.0 would rank an arm that failed to produce a number below one that produced a bad one.
    See .claude/decisions/2026-08-20-anti-memorization-and-search.md.
    """
    shared = tuple(sorted(set(head.row_scores) & set(candidate.row_scores)))
    holes = tuple(sorted(set(head.row_scores) - set(candidate.row_scores)))

    def _refused(blocker: str) -> SearchComparison:
        # Keyword form, not positional: dropping the `accepted` field would otherwise have shifted
        # every later argument silently, which is the class of defect this whole change removes.
        return SearchComparison(
            beats=False,
            head_score=None,
            candidate_score=None,
            shared_rows=shared,
            holes=holes,
            regressions=(),
            blocker=blocker,
        )

    if not head.row_scores:
        return _refused(
            "the lineage head scored no rows, so there is no bar to beat — record a head from a "
            + "round that measured something"
        )
    if not shared:
        return _refused(
            "the two rounds share no rows, so there is nothing to compare — a wiring fault rather "
            + "than a result. Pin `dataset.sample_seed` if the suite samples, and check both arms "
            + "mounted the snapshot you think they did."
        )
    if holes:
        return _refused(
            f"the candidate produced no score for {list(holes)}, which the head scored. A hole is "
            + "not a win: averaging over the survivors would reward the arm that failed on them. "
            + "Re-run before reading this."
        )

    head_score = mean([head.row_scores[r] for r in shared])
    candidate_score = mean([candidate.row_scores[r] for r in shared])
    beats = candidate_score > head_score

    regressions = tuple(regression_check(list(corpus), candidate, threshold=threshold)) if beats else ()
    blocker = None
    if regressions:
        lost = ", ".join(f"{row.row_id} ({row.reason})" for row, _ in regressions)
        blocker = (
            f"the candidate's train score improves but it re-loses {lost} — rows an earlier "
            + "promotion was built on. A search accept advances the lineage, so accepting this "
            + "carries the regression forward until a multi-arm round notices."
        )
    return SearchComparison(
        beats=beats,
        head_score=head_score,
        candidate_score=candidate_score,
        shared_rows=shared,
        holes=holes,
        regressions=regressions,
        blocker=blocker,
    )
