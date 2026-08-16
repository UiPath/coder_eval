"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic. Claude Code engages a skill via an explicit ``Skill`` tool call;
Codex has no such tool — it auto-discovers skills under ``.agents/skills/`` and
engages one by reading its ``SKILL.md`` / references off disk via shell. Both
signals are detected here so the criterion scores identically across agents, and
both require the signal to have actually DELIVERED the body: a refused, in-flight
or crash-force-closed ``Skill`` call, and a failed or unresolved
``Read``/``Glob``/``Grep``, loaded nothing and are not engagement.

**The delivered-body rule re-baselines every pre-existing activation suite DOWNWARD.** The same
traces that used to score ``yes`` for a ``Skill`` call with ``result_status: error`` now score
``no``, so a suite whose ``suite_thresholds`` were set before this rule is gated against numbers
its runs can no longer reach — and it fails without anything in the report saying the rule moved
rather than the skill. A suite authored before it must be RE-MEASURED before its thresholds are
trusted. ``framework_version`` in ``run.json`` is the only attribution a trend line across that
boundary gets; there is no per-criterion version stamp, so a chart spanning the change will show a
step that belongs to the checker, not to the agent.
"""

from __future__ import annotations

import logging
import re
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


def _engaged_skill_names(cmd: CommandTelemetry) -> set[str]:
    """All skill names engaged by ONE command, agent-agnostically (any-skill).

    Detects both engagement signals so the criterion scores identically across
    agents, and returns the (possibly empty) set of engaged skill names:

    - Claude: an explicit ``Skill`` tool call carries the skill in
      ``parameters['skill']``, optionally namespaced (e.g.
      ``plugin:uipath-agents``); the namespace is stripped via ``.split(":")[-1]``.
    - Codex (and any non-Claude agent): no ``Skill`` tool exists, so a skill is
      engaged by successfully reading its files off disk. Both the repo layout
      (``.../skills/<name>/...``) and the sandbox symlink
      (``.agents/skills/<name>/...``) contain the substring ``skills/<name>/``,
      matched here in any string parameter (Bash ``parameters['command']`` or a
      file-path parameter). The trailing separator required by ``_SKILL_PATH_RE``
      prevents prefix collisions (``uipath-agents`` vs ``uipath-agents-foo``).

    Returning the full set (rather than a single-skill yes/no) lets callers detect
    a *competing* skill engagement.
    """
    names: set[str] = set()
    if cmd.tool_name == "Skill":
        # ALLOWLIST, not a denylist: engagement counts only on a SUCCESSFUL call. For the
        # ``Skill`` tool the skill body IS the tool result, so anything other than a
        # delivered result means nothing loaded. The three excluded states share that:
        #   - "error"   — refused, typically ``disable-model-invocation: true`` ("cannot be
        #                 used with Skill tool due to disable-model-invocation"). The agent
        #                 proceeds on prior knowledge and still produces plausible output,
        #                 so nothing downstream looks wrong. Observed on 24 of 24 rows of a
        #                 real outcome suite, where it made an entire A/B round measure the
        #                 model's background knowledge instead of the skill.
        #   - None      — still in flight: the early-stop watcher evaluates on
        #                 ``ToolStartEvent``, before any result exists.
        #   - "unknown" — force-closed by a turn crash before any result arrived.
        if cmd.result_status != "success":
            logger.debug(
                "Skill call for %r did not deliver a body (result_status=%r, %s); not counting it as engagement",
                cmd.parameters.get("skill"),
                cmd.result_status,
                (cmd.result_summary or "")[:120],
            )
        else:
            skill = cmd.parameters.get("skill", "")
            if isinstance(skill, str) and skill:
                names.add(skill.split(":")[-1])
        # This branch is AUTHORITATIVE for a ``Skill`` call — return rather than fall
        # through to the file-read scan below, which would otherwise resurrect a call the
        # gate just excluded the moment one of its own parameters contained a
        # ``skills/<name>/``-shaped substring. That would restore the false `yes` and break
        # the monotonicity the latch depends on.
        return names
    # The file-read signal (Codex, or any agent reading a SKILL.md off disk) is scanned on
    # every non-``Skill`` command's string parameters, because the body really did reach
    # the agent.
    # It is gated only for the three tools whose failure means nothing was loaded: a failed
    # or in-flight ``Read``/``Glob``/``Grep`` puts the path in ``parameters`` while loading
    # nothing (a ``Grep`` that matched nothing exits non-zero and did not deliver a body
    # either — that is intended, not collateral damage). ``None`` is excluded for these
    # tools too: counting it would reintroduce the live/frozen divergence one branch over,
    # with the watcher live-passing on the ToolStart of a ``Read`` that then ENOENTs.
    # ``"unknown"`` still counts here — Codex reconstructs genuinely-executed calls with
    # that status — and every other tool, ``Bash`` included, is ungated, because a non-zero
    # exit does not imply the file was not read.
    if not (cmd.tool_name in _FILE_READ_TOOLS and cmd.result_status in ("error", None)):
        for value in cmd.parameters.values():
            if isinstance(value, str):
                names.update(_SKILL_PATH_RE.findall(value))
    return names


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
        return ClassificationCriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details=f"observed={observed!r}, expected={expected!r}{filt}",
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

        Latching is safe because engagement is computed only from RESOLVED
        telemetry for the tools whose failure means nothing loaded (a ``Skill``
        call must have succeeded; a ``Read``/``Glob``/``Grep`` must not have
        failed or be in flight — see ``_engaged_skill_names``). A command's
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
