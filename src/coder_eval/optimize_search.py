"""The search loop's accept/revert decision, and the anti-memorization preflight.

Rank 3 of the optimize family, beside :mod:`coder_eval.optimize_fronts` and importing nothing from
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
    """Train-row content a candidate reproduces verbatim AND its baseline does not.

    A preflight, not part of any verdict: it needs no runs, so it is read at proposal time, before
    Stage A is paid for. Distinct from ``regression_check`` beside it, which asks whether an arm
    *re-lost a measured row* and therefore cannot be answered without run results.

    **A DIFF, not an absolute scan, and the difference is what makes it usable.** Measured against
    this repo's own ``tasks/skills/ci-outcome.yaml`` on its TRAIN split, an absolute scan flags the
    shipped ``ci`` skill on five strings — ``minimum-task-score``, ``persist-credentials: false``
    and three more — none of which is memorization: that body legitimately documents the output
    contract its suite grades. A checker that fires on the shipped skill on its first run is one
    users learn to ignore. What is worth flagging is what a candidate NEWLY absorbs.

    ``baseline_text`` is **the text this candidate was derived from**, which is not always the
    incumbent: from round 2 a search-loop candidate is built on the *lineage head*, and diffing
    that against the incumbent would re-report every span the head added, every round. Pass the
    arm the candidate was actually edited from.

    **Five boundaries, stated so an empty result is not mistaken for a proof:**

    - It catches the VERBATIM form, as CE036 states of its own. A candidate that describes a train
      row's content in other words is a semantic leak and needs a reader. (Matching is
      case-insensitive on both sides, so casing alone does not evade it.)
    - Containment is a **substring** test in both directions, so a graded value can be masked by an
      unrelated baseline substring that happens to contain it, and flagged for a subword
      occurrence. The :data:`~coder_eval.leak_detection.LEAK_MIN_CHARS` floor makes both unlikely
      rather than impossible.
    - **A span already in the baseline is invisible from here on.** That is right while the
      baseline is the user's shipped skill, which is the measured case above — but from round 2 the
      baseline is itself a former candidate, so a memorized span that rode into a promotion
      alongside a genuine improvement is never flagged again. The proposer-side rule in
      ``reference/proposal-prompt.md`` is what covers that; this function cannot.
    - **The gold solution is out of reach.** Only ``row.success_criteria`` is scanned.
      ``TaskDefinition.reference`` — the reference solution ``reference_comparison`` / ``llm_judge``
      / ``agent_judge`` score against — is a task-level field, and it may name a file or a whole
      directory rather than carry its text, so scanning it would mean reading the filesystem from
      what is otherwise a pure function. That matters more than it looks: ``proposal-prompt.md``
      tells the proposer to *study* the reference, and calls copying it "especially tempting"
      because the content is known-correct. This checker cannot see that copy. A reader has to.
    - **The caller decides what text is scanned, and this function cannot tell how much of the
      candidate it saw.** Handed one file's contents it checks one file, and a candidate that bundles
      train-row content into ``scripts/`` or a reference file comes back CLEAN — byte-identical to a
      genuinely clean one, which is the worst shape a preflight can have. :func:`skill_text` above is
      what a caller should pass, and it is a separate function precisely so this one stays pure:
      widening the scan is an IO decision, and pushing it in here would make the checker read the
      filesystem.

    ``rows`` are the EXPANDED row-tasks of the TRAIN split only — passing the whole suite would
    flag content drawn from rows the candidate is entitled to be fitted to. (The five-string
    figure above is the train split's; the whole suite gives seven.)

    Findings are de-duplicated, preserving order. A suite may assert the same string twice on a
    row — this repo's own does — and repeating the line says nothing the first one did not, in a
    check whose entire design rationale is not firing more than it has to.
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
    """Whether the search loop should carry ``candidate`` forward in place of ``head``.

    **Not a gate.** The two means come from different invocations, unpaired, unreplicated and
    uncorrected — the arithmetic the promotion gate exists to distrust. A ``True`` here is a
    hypothesis to gate, never a result, and nothing in this function promotes anything.

    It exists as a function rather than as arithmetic in the skill's prose because each guard
    below only works if it is applied, and the previous home for them was a markdown block an
    agent copies and adapts:

    - **The comparison runs over the rows BOTH arms scored, and nothing else.** ``head``'s vector
      was recorded in an earlier round and ``candidate``'s comes from the run just paid for, so
      nothing guarantees they cover the same rows — and every way they diverge favours the
      candidate.
    - **No overlap at all is reported before holes are**, because it is a *wiring* fault (an
      unpinned ``dataset.sample_seed`` draws a different sample across invocations) and calling it
      a hole sends the reader hunting a flaky row.
    - **A hole refuses rather than averaging around it.** A candidate that errored on the hardest
      rows scores a higher mean over the survivors; that is the rule :func:`_dominates` already
      applies to the row matrix. A refused comparison reports ``None`` for both scores rather than
      a number nobody should read.
    - **A corpus regression blocks an otherwise-winning candidate.** A search accept advances the
      lineage, so a row an earlier promotion was built on would be re-lost and carried forward
      until the next multi-arm round noticed. An aggregate cannot show that — the whole premise of
      the corpus — and the check is free here because ``regression_check`` takes exactly the arm
      this function already has.

    A tie does not win: ``beats`` requires strictly greater. Advancing the head on a tie moves the
    bar every later round is judged against, on an accident.

    ``corpus`` and ``threshold`` are forwarded to :func:`regression_check`; the default of 1.0
    treats any partial score as a loss, which is right for the binary activation criterion the
    corpus is usually written from.
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
