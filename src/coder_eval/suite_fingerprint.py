"""A content digest over the parts of a suite that decide what a score MEANS.

The grader fingerprint (``verify.py --fingerprint``) covers the outcome track's *script* and its
answer key. This covers the suite around it — the criteria, the prompt, the rows themselves and the
run limits — so two rounds' scores are comparable only when both digests agree. It is
**track-independent**, and on the activation track it is the ONLY instrument provenance there is:
that track has no script grader at all.

**Its own module because the store PERSISTS and this COMPUTES.** That is the precedent
:mod:`coder_eval.leak_detection` and :mod:`coder_eval.reports_optimize` already record in
``CLAUDE.md``, and it is the whole reason — **not** a dependency cycle, which does not exist:
:mod:`coder_eval.optimize_store` imports :mod:`coder_eval.models` and nothing else from the package,
:class:`~coder_eval.models.TaskDefinition` is in ``coder_eval.models``, so a digest living in the
store would close no cycle. What it would do is make a persistence module the derivation site for
the value it stores, which cannot then be reviewed as one thing. :mod:`coder_eval.optimize_load` is
the other near-fit and is wrong for a different reason: its charter is every question answered by
READING a finalized run directory, and this is computed from task definitions before any run exists.

**The digest, and nothing else.** Never the pre-image, and never a field-level diff of VALUES: a
``measurements.json`` is committed, so a "what changed?" affordance would have to name field *paths*
rather than values or it becomes a leak surface. This is a one-way content digest for change
detection — not signing, not key material, not a randomness source.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from coder_eval.models import BaseSuccessCriterion, RunLimits, TaskDefinition


# Criterion keys excluded from the digest, mapped to the REASON — CE038's reason-carrying ``EXEMPT``
# shape, so a stale licence cannot outlive the trade it recorded.
#
# **A denylist, deliberately, and this is the one place in this family where that is the safe
# direction.** An allowlist over base fields is what the first draft of this specified, and it could
# not have seen a `run_command.command` move from `verify.py` to `verify2.py` — the precise failure
# the grader fingerprint exists to catch, reintroduced one level up. Every parameter that says what a
# criterion MEASURES lives on the concrete subclass, so the digest hashes the subclass dump WHOLE and
# a new parameter is included by default. Excluding one is then always a decision someone made.
_NOT_SCORING_RELEVANT: dict[str, str] = {
    "description": "authored prose; changes no score",
}

# One tag absorbed before each section's parts.
#
# What they are for, stated exactly, because an earlier draft of this comment overclaimed: the
# collision they close is a part MIGRATING between sections — a suite whose prompt equals another
# suite's criterion dump hashing identically to that other suite with the criterion moved into the
# prompt. That collision was real when the prompt was absorbed RAW; it is already closed by
# :func:`_canonical`, since a mapping's canonical form begins ``{`` and a string's begins ``"`` and
# the two can never be equal. The tags stay because they make that argument LOCAL — injectivity then
# does not depend on every part's JSON type staying distinguishable from every other section's, which
# is a property a future part absorbed as a bare string would quietly break.
#
# Deliberately NOT accompanied by a part COUNT per section. A count is redundant given
# length-prefixed parts (a section with one more part is a different byte stream either way), and a
# redundant guard whose rationale has to be qualified is worse than no guard: the next reader cannot
# tell which of the two is doing the work.
_SECTIONS = ("criteria", "prompt", "rows", "run_limits")


def _canonical(value: object) -> str:
    """``value`` as canonical JSON: keys sorted at every depth, no incidental whitespace.

    ``json.dumps`` rather than ``repr``, because ``sort_keys=True`` already sorts nested mappings —
    so a YAML re-ordering of ``suite_thresholds`` is not a changed instrument — and because a repr is
    a Python-version detail while this string is hashed into a value compared across checkouts.

    It also removes the need for an "unset" sentinel: ``None`` renders as ``null`` while the *string*
    ``"null"`` renders as ``"null"`` **with quotes**, so absent and present-but-empty cannot collide.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def scoring_dump(criterion: BaseSuccessCriterion) -> dict[str, object]:
    """One criterion's contribution to the digest: its CONCRETE dump minus the denylist.

    Public so a test can assert the contract directly — that every key of the plain
    ``model_dump(mode="json")`` except the denylisted ones reaches the digest. The real rot path here
    is someone adding ``exclude_none=True`` / ``exclude_defaults=True`` / ``exclude_unset=True`` to
    the dump, each of which silently drops fields; ``exclude_defaults`` drops the ``type``
    discriminator itself, after which two criteria of DIFFERENT types collide outright. A test
    comparing two digests for inequality cannot see any of that — dropping a key from one side of an
    inequality still leaves the two unequal — so the contract is asserted on this function instead.
    """
    dumped = criterion.model_dump(mode="json")
    return {key: value for key, value in dumped.items() if key not in _NOT_SCORING_RELEVANT}


def suite_fingerprint(task: TaskDefinition, rows: Sequence[TaskDefinition]) -> str:
    """A SHA-256 digest of the scoring-relevant parts of one suite and the rows it scored.

    ``task`` is the **UNEXPANDED** suite task — what
    :func:`~coder_eval.orchestration.task_loader.load_task` returns, *before* ``expand_dataset``. Its
    ``${row.<field>}`` placeholders are hashed as authored, which is correct: a changed template is a
    changed instrument.

    ``rows`` are the **EXPANDED row-tasks the round actually scored**, and they are the half that
    makes this the activation track's instrument rather than a partial reading of it. On the shipped
    ``activation.yaml`` the template is ``initial_prompt: ${row.prompt}`` with
    ``expected_skill: "${row.expected_skill}"`` — every prompt *and every label* lives in the rows
    file, so a digest over row IDS alone is blind to a rewritten prompt and a flipped label, which is
    the commonest suite edit there is. The grader half of this pair hashes ``expectations/*.json``
    "because the answer key is part of the instrument"; this is the same rule on the other track.
    Pass ``()`` for a suite with no ``dataset:`` — the template is then the whole instrument.

    What is covered, in this order, each section tagged and counted: every criterion's
    :func:`scoring_dump` in declaration order; the initial prompt; each row's id, prompt and
    criteria, sorted by row id; and the whole ``run_limits`` block.

    What is NOT: the **task-level** agent and sandbox blocks, and therefore the paths in them — an
    absolute ``template_dir`` or a ``$SKILL_SOURCE_PATH`` would otherwise report "the instrument
    moved" on a colleague's checkout, the exact false positive ``verify.py::fingerprint`` avoids by
    hashing relative paths. Note the boundary that follows from hashing criteria WHOLE: an
    ``agent_judge`` criterion embeds its own agent config, so a judge's model, plugin paths and
    settings ARE hashed. That is right on the merits — the judge's model is part of what that
    criterion measures — but it means a suite whose judge names a machine-local path has a
    machine-local digest, and cross-checkout stability is a property of suites that do not.

    ``run_limits`` is hashed WHOLE rather than through a curated list of caps. An earlier draft named
    four (``max_turns``, ``turn_timeout``, ``task_timeout``, ``max_usd``) and justified excluding the
    rest as "wiring that bounds a run without changing what a completed row scores", which is false
    twice over: the three token caps abort a run exactly as ``max_usd`` does, and
    ``run_limits.stop_early`` is the kill switch for every armed criterion — flipping it changes
    ``f1.yes``, the metric the activation gate promotes on. An absent block hashes as the DEFAULT
    block, because that is what an absent block means.

    Order-SENSITIVE across criteria, because ``criterion_index`` is positional everywhere in the
    optimize family: swapping two criteria changes which one a gate reads. Order-INSENSITIVE within a
    mapping (``sort_keys`` at every depth) and across rows, which are sorted by id — a different draw
    ORDER is not a different row set, while a different SET is.

    Each part is hashed with its byte LENGTH, exactly as ``verify.py::fingerprint`` does — and here it
    is redundancy rather than the primary defence, which is worth saying so nobody reasons from a
    guarantee the wrong mechanism provides. Every variable part goes through :func:`_canonical`, which
    escapes a NUL as ``\u0000``, so no value can contain the delimiter to forge in the first place.
    The prefix stays because it makes the framing's correctness local to ``absorb`` instead of
    dependent on every future part continuing to be JSON.
    """
    digest = hashlib.sha256()

    def absorb(part: str) -> None:
        encoded = part.encode("utf-8")
        digest.update(str(len(encoded)).encode("ascii"))
        digest.update(b"\0")
        digest.update(encoded)

    criteria_tag, prompt_tag, rows_tag, limits_tag = _SECTIONS

    absorb(criteria_tag)
    for criterion in task.success_criteria:
        absorb(_canonical(scoring_dump(criterion)))

    absorb(prompt_tag)
    absorb(_canonical(task.initial_prompt))

    absorb(rows_tag)
    ordered = sorted(rows, key=lambda row: row.row_id or "")
    for row in ordered:
        absorb(_canonical(row.row_id))
        absorb(_canonical(row.initial_prompt))
        absorb(_canonical([scoring_dump(criterion) for criterion in row.success_criteria]))

    absorb(limits_tag)
    # `or RunLimits()`: an absent block means the defaults apply, so it must hash as the defaults do
    # rather than as a third state beside "declared" and "declared empty".
    absorb(_canonical((task.run_limits or RunLimits()).model_dump(mode="json")))
    return digest.hexdigest()


def stale_denylist_keys() -> set[str]:
    """:data:`_NOT_SCORING_RELEVANT` keys that are not ``BaseSuccessCriterion`` fields.

    Empty in a healthy tree. It exists so the denylist can be checked against the model rather than
    against a second list: a key that stops matching a real field is a licence that has outlived the
    trade it recorded, which is CE038's ``EXEMPT`` lesson.

    Scoped to the BASE criterion, so a deliberate exclusion of a *subclass* field would be reported
    here as stale. There is none today, and adding one should be a decision this function forces.
    """
    return set(_NOT_SCORING_RELEVANT) - set(BaseSuccessCriterion.model_fields)
