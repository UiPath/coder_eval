"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic. Claude Code engages a skill via an explicit ``Skill`` tool call;
Codex has no such tool — it auto-discovers skills under ``.agents/skills/`` and
engages one by reading its ``SKILL.md`` / references off disk via shell. Both
signals are detected here so the criterion scores identically across agents, and
both require the signal to have actually DELIVERED the body: a ``Skill`` call and a
``Read``/``Glob``/``Grep`` are engagement on ``result_status == "success"`` and on nothing
else, so a refused, in-flight or crash-force-closed one of either loaded nothing.

**The delivered-body rule re-baselines every pre-existing activation suite DOWNWARD.** The same
traces that used to score ``yes`` for a ``Skill`` call with ``result_status: error`` now score
``no``, so a suite whose ``suite_thresholds`` were set before this rule is gated against numbers
its runs can no longer reach — and it fails without anything in the report saying the rule moved
rather than the skill. A suite authored before it must be RE-MEASURED before its thresholds are
trusted. ``framework_version`` in ``run.json`` is the only attribution a trend line across that
boundary gets; there is no per-criterion version stamp, so a chart spanning the change will show a
step that belongs to the checker, not to the agent.

**The file-read half moved in the same direction on 2026-08-16**, and a trend line spanning
either change looks the same, so both are recorded here. ``Read``/``Glob``/``Grep`` were gated
by a DENYLIST (``result_status in ("error", None)``) while the ``Skill`` tool beside them used
an ALLOWLIST, so a crash-force-closed ``"unknown"`` file read counted as engagement while the
identical crash on a ``Skill`` call did not. Both are the allowlist now, expressed once in
``_delivered``.

**Nothing recorded re-scores, and the denominator that shows it is the FILE-READ one.** A count
over all commands would be near-vacuous here: Codex emits none of ``Read``/``Glob``/``Grep`` (see
``TestPerAgentTelemetryInventory``), so every Codex command in a corpus is structurally incapable
of carrying the changed pair and inflates the null. Measured instead over the backend that CAN
produce it — Claude Code, whose ``_finalize_commands`` is what force-closes to ``"unknown"`` —
across 1,754 ``task.json`` on the authoring machine: **8,706 file-read commands, 8,496
``"success"``, 210 ``"error"``, 0 ``"unknown"``.** So this closes a LATENT false positive before
it reached a promotion decision rather than after, and no suite re-baselines. The caveat that
remains: Antigravity also reaches these tool names and is barely represented in that corpus, so
the null is a claim about recorded CLAUDE traces, not a proof the pair is unreachable.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING

from coder_eval.criteria._classification_aggregate import overlay_classification_metrics
from coder_eval.criteria.base import BaseCriterion, LiveVerdict, register_criterion
from coder_eval.models import (
    TARGET_LABEL,
    ClassificationCriterionResult,
    CriterionAggregate,
    CriterionResult,
    SkillTriggeredCriterion,
)


if TYPE_CHECKING:
    from coder_eval.criteria.base import CheckContext
    from coder_eval.models.results import TurnRecord
    from coder_eval.models.telemetry import CommandTelemetry
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# ONE declaration of the positive label. The criterion PRODUCES it and `optimize_gate` CONSUMES
# it (its activation gate reads `f1.yes`), so the two must agree — but the dependency has to point
# this way: `models/optimize.py` is a cycle-free leaf that `optimize_gate` imports, and `models`
# cannot import `criteria`. `_NO` is left as a literal; it has no twin anywhere.
_YES = TARGET_LABEL
_NO = "no"

# Extracts the skill name between ``skills/<name>/`` path segments (the Codex
# file-read signal). Accept both POSIX and Windows separators because telemetry
# records the command exactly as the agent emitted it. One-or-more separators
# also handles JSON-escaped commands that retain doubled backslashes. The
# lookahead permits overlapping matches such as ``.../skills/skills/<name>/...``.
_SKILL_PATH_RE = re.compile(r"(?=skills[\\/]+([A-Za-z0-9][A-Za-z0-9_-]*)[\\/]+)")

# File-read tools whose FAILURE means nothing was loaded. ``Bash`` is deliberately absent:
# ``cat skills/x/SKILL.md | grep foo`` exits 1 after genuinely reading the file, so a status
# gate there would drop real Codex engagement. Antigravity IS covered — its tool map
# (``antigravity_agent.py``) renames ``view_file``/``search_directory``/``find_file`` to
# ``Read``/``Grep``/``Glob``, so its file-read engagement decides at the ToolEnd rather than
# the ToolStart: one evaluation round later, not a lost signal.
_FILE_READ_TOOLS = frozenset({"Read", "Glob", "Grep"})

# How many distinct ``tool/status`` pairs the non-engagement note in ``details`` renders before
# eliding the rest. ``details`` is persisted per row and rendered in reports, so the note is
# bounded rather than proportional to the trajectory.
_SUPPRESSED_RENDER_LIMIT = 3


def _delivered(cmd: CommandTelemetry) -> bool:
    """Did this command actually deliver the skill body?

    ONE declaration of the delivered-body rule, applied identically to the ``Skill`` tool
    (whose result IS the body) and to the file-read tools (whose failure means nothing
    loaded). It is an ALLOWLIST — engagement counts on ``"success"`` and on nothing else —
    because a new ``result_status`` value must not fall into the engagement bucket by
    default. The three currently-reachable exclusions share one meaning:

      - "error"   — for ``Skill``, typically a refusal under ``disable-model-invocation:
                    true`` ("cannot be used with Skill tool due to disable-model-invocation");
                    for a file read, an ENOENT or a ``Grep`` that matched nothing. Either
                    way the agent proceeds on prior knowledge and still produces plausible
                    output, so nothing downstream looks wrong. Observed on 24 of 24 rows of
                    a real outcome suite, where it made an entire A/B round measure the
                    model's background knowledge instead of the skill.
      - None      — still in flight: the early-stop watcher evaluates on ``ToolStartEvent``,
                    before any result exists. Counting it would live-pass on the ToolStart
                    of a ``Read`` that then ENOENTs.
      - "unknown" — force-closed by a turn crash before any result arrived
                    (``claude_code_agent._finalize_commands``; ``antigravity_agent``'s orphan
                    close). No result ever reached the agent.

    Every other tool — ``Bash`` included — is ungated, because a non-zero exit does not
    imply the file was not read: ``cat SKILL.md | grep foo`` exits 1 AFTER genuinely reading
    it, and that is the whole off-Claude file-read signal.
    """
    if cmd.tool_name == "Skill" or cmd.tool_name in _FILE_READ_TOOLS:
        return cmd.result_status == "success"
    return True


def _candidate_skill_names(cmd: CommandTelemetry) -> set[str]:
    """Skill names this command WOULD engage, ignoring whether it delivered.

    Detects both engagement signals so the criterion scores identically across agents:

    - Claude: an explicit ``Skill`` tool call carries the skill in ``parameters['skill']``,
      optionally namespaced (e.g. ``plugin:uipath-agents``); the namespace is stripped via
      ``.split(":")[-1]``.
    - Codex (and any non-Claude agent): no ``Skill`` tool exists, so a skill is engaged by
      reading its files off disk. Both the repo layout (``.../skills/<name>/...``) and the
      sandbox symlink (``.agents/skills/<name>/...``) contain the substring
      ``skills/<name>/``, matched in any string parameter (Bash ``parameters['command']`` or
      a file-path parameter). The trailing separator required by ``_SKILL_PATH_RE`` prevents
      prefix collisions (``uipath-agents`` vs ``uipath-agents-foo``).

    The ``Skill`` branch is AUTHORITATIVE for a ``Skill`` call — it returns rather than also
    running the path scan, which would otherwise resurrect a call the gate excluded the
    moment one of its own parameters contained a ``skills/<name>/``-shaped substring. That
    would restore the false `yes` and break the monotonicity the latch depends on.
    """
    if cmd.tool_name == "Skill":
        skill = cmd.parameters.get("skill", "")
        return {skill.split(":")[-1]} if isinstance(skill, str) and skill else set()
    return {
        name for value in cmd.parameters.values() if isinstance(value, str) for name in _SKILL_PATH_RE.findall(value)
    }


def _engaged_skill_names(cmd: CommandTelemetry) -> set[str]:
    """All skill names engaged by ONE command, agent-agnostically (any-skill).

    Returns the (possibly empty) set of engaged skill names — the full set rather than a
    single-skill yes/no, so callers can detect a *competing* skill engagement.

    The delivered-body gate and the name extraction are deliberately separate functions:
    the gate used to be written twice (an allowlist on the ``Skill`` branch, a denylist on
    the file-read one), the two drifted, and the drift was a false `yes` on every
    crash-force-closed file read. One expression cannot disagree with itself.
    """
    if _delivered(cmd):
        return _candidate_skill_names(cmd)
    logger.debug(
        "%s call did not deliver a skill body (result_status=%r, %s); not counting it as engagement",
        cmd.tool_name,
        cmd.result_status,
        (cmd.result_summary or "")[:120],
    )
    return set()


def _suppressed_engagements(turn_records: list[TurnRecord], skill_name: str) -> Counter[str]:
    """``tool/status`` pairs that WOULD have engaged ``skill_name`` but delivered nothing.

    DIAGNOSTIC ONLY — the return value reaches ``details`` and nothing else. It is the exact
    complement of ``_engaged_skill_names``, built by calling ``_delivered`` and
    ``_candidate_skill_names`` rather than re-deriving either, so the reason it reports is
    necessarily the same rule the score applied. A third spelling of the delivered-body
    predicate is precisely the drift that produced the defect this file was last fixed for.

    Why it is worth reporting at all: a suite whose ``recall.yes`` collapsed because every
    ``Skill`` call was refused under ``disable-model-invocation`` is indistinguishable, in the
    report, from one where the agent simply never reached for the skill. The first is a wiring
    fault that invalidates the round; the second is the measurement working.
    """
    return Counter(
        f"{cmd.tool_name}/{cmd.result_status or 'in-flight'}"
        for turn in turn_records
        for cmd in turn.commands
        if not _delivered(cmd) and skill_name in _candidate_skill_names(cmd)
    )


def _render_suppressed(suppressed: Counter[str]) -> str:
    """The ``details`` suffix for a non-engagement, or ``""`` when nothing was suppressed.

    Bounded on purpose: a run with fifty refused calls must not produce a fifty-entry string,
    since ``details`` is persisted per row in ``task.json`` and rendered in reports. Aggregated
    by ``(tool, status)`` with counts, top ``_SUPPRESSED_RENDER_LIMIT`` pairs, remainder elided.

    Neutrally worded, and that is not cosmetic: on a DISTRACTOR criterion a suppressed
    engagement means the row correctly scored ``no`` and PASSED, so "suppressed" must read as an
    observation rather than a fault. Uses ``x`` and parentheses rather than ``[...]`` — this
    string reaches renderers that interpret square brackets as markup.
    """
    if not suppressed:
        return ""
    ranked = suppressed.most_common()
    shown = ", ".join(f"{pair} x{count}" for pair, count in ranked[:_SUPPRESSED_RENDER_LIMIT])
    elided = "" if len(ranked) <= _SUPPRESSED_RENDER_LIMIT else f", +{len(ranked) - _SUPPRESSED_RENDER_LIMIT} more"
    return f" — {sum(suppressed.values())} engagement signal(s) not delivered: {shown}{elided}"


def _all_engaged_skill_names(turn_records: list[TurnRecord]) -> set[str]:
    """Union of every skill engaged anywhere in the trajectory (any-engagement).

    Activation is scored on whether a skill was engaged *at all* during the run,
    not on which skill was engaged first. This has two consequences that the
    first-engagement policy could not express:

    - **Recall (the positive criterion).** A row that engages the wrong skill
      before eventually engaging the expected one is still credited for the
      expected skill — reading a ``SKILL.md`` to compare candidates is
      exploration, not commitment, so an earlier wrong touch must not fail the
      row.
    - **Precision (the distractor/negative criteria).** An unrelated skill
      engaged *anywhere* is counted against its own criterion, so a positive row
      that also fires an off-target skill (and a negative row that fires any
      target skill) is penalized on that skill's confusion cell.

    Order is irrelevant to a set union; the scan is left in ``sequence_number``
    order purely for determinism.
    """
    names: set[str] = set()
    for turn in turn_records:
        for cmd in turn.commands:
            names.update(_engaged_skill_names(cmd))
    return names


@register_criterion
class SkillTriggeredChecker(BaseCriterion[SkillTriggeredCriterion]):
    """Binary classifier: observed='yes' when the agent engaged the target skill.

    Returns a ``ClassificationCriterionResult`` so the suite aggregator can
    compute accuracy / recall / F1 / confusion matrix across all rows.
    """

    criterion_type = "skill_triggered"

    def _check_impl(
        self,
        criterion: SkillTriggeredCriterion,
        sandbox: Sandbox,
        *,
        turn_records: list[TurnRecord] | None = None,
        context: CheckContext | None = None,
    ) -> CriterionResult:
        if turn_records is None:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                details="No turn records available",
                error="turn_records not provided to checker",
            )

        # Any-engagement policy (mirrors ``live_verdict``): the row is scored on
        # whether this skill was engaged AT ALL, regardless of order. A positive
        # criterion (skill_name == expected_skill) passes iff the expected skill
        # was engaged somewhere in the run — a wrong skill engaged first does not
        # fail it (recall). A distractor/negative criterion fails on ANY
        # engagement of its skill (precision).
        triggered: bool = criterion.skill_name in _all_engaged_skill_names(turn_records)
        expected_yes: bool = criterion.expected_skill == criterion.skill_name
        score = 1.0 if triggered == expected_yes else 0.0
        observed = _YES if triggered else _NO
        expected = _YES if expected_yes else _NO

        filt = f" (skill_name={criterion.skill_name!r})"
        # Diagnostic suffix only, and ONLY on the non-engagement path: when the skill WAS
        # engaged, an earlier refused call is not a finding — a later success is engagement, and
        # reporting the suppression there would read as a failure. `score`, `observed_label` and
        # `expected_label` above are computed before this line and are untouched by it, and the
        # prefix is byte-identical to what it was, so nothing that reads the leading
        # `observed=…, expected=…` sees a change. (`criteria/classification_match.py` emits the
        # same prefix with no shared formatter — deliberately left duplicated: nothing parses
        # either, and extracting one for a single appending caller is abstraction for its own
        # sake.)
        suppressed = (
            "" if triggered else _render_suppressed(_suppressed_engagements(turn_records, criterion.skill_name))
        )
        return ClassificationCriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=f"observed={observed!r}, expected={expected!r}{filt}{suppressed}",
            observed_label=observed,
            expected_label=expected,
        )

    def live_verdict(
        self,
        criterion: SkillTriggeredCriterion,
        turn_records: list[TurnRecord],
    ) -> LiveVerdict:
        """Any-engagement latch: decide the instant THIS skill is engaged.

        Mirrors ``_check_impl``'s any-engagement policy, latched monotonically
        over the growing partial trajectory:

        - this skill not engaged yet -> ``"undecided"`` (the final outcome still
          depends on the rest of the run — the expected skill may load later, or
          a distractor may yet fire);
        - this skill engaged -> ``expected_skill == skill_name`` decides:
          ``"pass"`` for a positive criterion (the expected skill loaded),
          ``"fail"`` for a distractor/negative one (a wrong skill loaded).

        Latching is safe because engagement is computed only from RESOLVED,
        SUCCESSFUL telemetry for the tools whose failure means nothing loaded (a
        ``Skill`` call and a ``Read``/``Glob``/``Grep`` alike must have succeeded
        — one rule, ``_delivered``). A command's
        contribution can therefore only go absent -> present as it resolves,
        never present -> absent, so a latched verdict never flips and agrees with
        ``_check_impl`` on the frozen trajectory — whether or not the run stopped
        early. The cost is that a ``Skill`` engagement decides at its
        ``ToolEndEvent`` rather than its ``ToolStartEvent``: one evaluation round
        later, and no stop at all if the turn is cut between the two — in which
        case the frozen check scores that call ``no`` as well, which is the point.
        A positive criterion can therefore only ever live-``pass`` and a
        distractor/negative one only ever live-``fail``; their *absence* is never
        decidable mid-run (see ``SkillTriggeredCriterion.live_decidable_polarities``
        in models/criteria.py). This is the change from first-engagement: a wrong
        skill engaged first no longer live-fails a positive row — the run keeps
        going so the expected skill can still load.
        """
        if criterion.skill_name not in _all_engaged_skill_names(turn_records):
            return "undecided"
        return "pass" if criterion.expected_skill == criterion.skill_name else "fail"

    def aggregate(
        self,
        criterion: SkillTriggeredCriterion,
        per_row_results: list[CriterionResult],
    ) -> CriterionAggregate | None:
        """Baseline stats (from super) + classification overlay (accuracy/F1/...)."""
        base = super().aggregate(criterion, per_row_results)
        if base is None:
            return None
        return overlay_classification_metrics(base, per_row_results)
