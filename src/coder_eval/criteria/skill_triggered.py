"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic. Claude Code engages a skill via an explicit ``Skill`` tool call;
Codex has no such tool — it auto-discovers skills under ``.agents/skills/`` and
engages one by reading its ``SKILL.md`` / references off disk via shell. Both
signals are detected here so the criterion scores identically across agents.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, ClassVar

from coder_eval.criteria._classification_aggregate import overlay_classification_metrics
from coder_eval.criteria.base import BaseCriterion, LiveVerdict, register_criterion
from coder_eval.models import (
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

_YES = "yes"
_NO = "no"

# Extracts the skill name between ``skills/<name>/`` path segments (the Codex
# file-read signal). Accept both POSIX and Windows separators because telemetry
# records the command exactly as the agent emitted it. One-or-more separators
# also handles JSON-escaped commands that retain doubled backslashes. The
# lookahead permits overlapping matches such as ``.../skills/skills/<name>/...``.
_SKILL_PATH_RE = re.compile(r"(?=skills[\\/]+([A-Za-z0-9][A-Za-z0-9_-]*)[\\/]+)")


def _engaged_skill_names(cmd: CommandTelemetry) -> set[str]:
    """All skill names engaged by ONE command, agent-agnostically (any-skill).

    Detects both engagement signals so the criterion scores identically across
    agents, and returns the (possibly empty) set of engaged skill names:

    - Claude: an explicit ``Skill`` tool call carries the skill in
      ``parameters['skill']``, optionally namespaced (e.g.
      ``plugin:uipath-agents``); the namespace is stripped via ``.split(":")[-1]``.
    - Codex (and any non-Claude agent): no ``Skill`` tool exists, so a skill is
      engaged by reading its files off disk via shell. Both the repo layout
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
        skill = cmd.parameters.get("skill", "")
        if isinstance(skill, str) and skill:
            names.add(skill.split(":")[-1])
    for value in cmd.parameters.values():
        if isinstance(value, str):
            names.update(_SKILL_PATH_RE.findall(value))
    return names


def _first_engaged_skill_names(turn_records: list[TurnRecord]) -> set[str]:
    """Skills engaged by the FIRST command that engages any skill (else empty).

    Activation measures which skill the agent selects *first*, so scoring keys off
    the first engaging command rather than "any command anywhere in the run". This
    keeps the final check and the live verdict consistent and prevents a later,
    incidental engagement (or a second skill invoked alongside the first) from
    being counted as a competing activation and mis-scored as a false positive.
    Commands are scanned in ``sequence_number`` order (turn records preserve it).
    """
    for turn in turn_records:
        for cmd in turn.commands:
            engaged = _engaged_skill_names(cmd)
            if engaged:
                return engaged
    return set()


@register_criterion
class SkillTriggeredChecker(BaseCriterion[SkillTriggeredCriterion]):
    """Binary classifier: observed='yes' when the agent engaged the target skill.

    Returns a ``ClassificationCriterionResult`` so the suite aggregator can
    compute accuracy / recall / F1 / confusion matrix across all rows.
    """

    criterion_type = "skill_triggered"

    # Observable mid-run: a Skill tool call (or a skill file read) is a positive
    # event in the live stream, so both polarities are decidable the moment the
    # agent first engages ANY skill (the first-engagement policy in live_verdict).
    live_stop_polarities: ClassVar[frozenset[str]] = frozenset({"pass", "fail"})

    def _check_impl(
        self,
        criterion: SkillTriggeredCriterion,
        sandbox: Sandbox,
        reference_code: str | None = None,
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

        # First-engagement policy (mirrors ``live_verdict``): the run is scored on
        # the FIRST skill the agent engages, so a second skill invoked alongside or
        # after it is not counted as a competing activation.
        triggered: bool = criterion.skill_name in _first_engaged_skill_names(turn_records)
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
        """First-engagement policy: the FIRST observed skill engagement decides.

        Activation measures which skill the agent selects *first*, so every
        stacked ``skill_triggered`` criterion is decided simultaneously by the
        first command that engages any skill:

        - before any engagement -> ``"undecided"``;
        - on the first command engaging some skill: ``observed = (skill_name in
          engaged)``, ``expected = (expected_skill == skill_name)`` ->
          ``"pass"`` iff they match, else ``"fail"``.

        This covers the "wrong skill loads" case (a positive wrong signal) and
        negative rows (``expected_skill == ""`` -> any engagement of the target
        fails that criterion). ``_check_impl`` applies the SAME first-engagement
        policy on the full trajectory, so the live verdict and the authoritative
        score agree by construction — whether or not the run stopped early.
        """
        engaged = _first_engaged_skill_names(turn_records)
        if not engaged:
            return "undecided"
        expected_yes = criterion.expected_skill == criterion.skill_name
        observed_yes = criterion.skill_name in engaged
        return "pass" if observed_yes == expected_yes else "fail"

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
