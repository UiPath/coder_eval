"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic. Claude Code engages a skill via an explicit ``Skill`` tool call;
Codex has no such tool — it auto-discovers skills under ``.agents/skills/`` and
engages one by reading its ``SKILL.md`` / references off disk via shell. Both
signals are detected here so the criterion scores identically across agents.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from coder_eval.criteria._classification_aggregate import overlay_classification_metrics
from coder_eval.criteria.base import BaseCriterion, register_criterion
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


def _engaged_skill(cmd: CommandTelemetry, skill_name: str) -> bool:
    """True when one command engaged ``skill_name`` — agent-agnostically.

    Claude: an explicit ``Skill`` tool call carries the skill in
    ``parameters['skill']`` (optionally namespaced, e.g. ``plugin:uipath-agents``).

    Codex (and any non-Claude agent): no ``Skill`` tool exists, so the skill is
    engaged by reading its files off disk via shell. Both the repo layout
    (``.../skills/<skill_name>/...``) and the sandbox symlink
    (``.agents/skills/<skill_name>/...``) contain the substring
    ``skills/<skill_name>/``, which appears in the recorded command string
    (Bash ``parameters['command']``) or a file-path parameter. The trailing
    slash prevents prefix collisions (``uipath-agents`` vs ``uipath-agents-foo``).
    """
    if cmd.tool_name == "Skill" and cmd.parameters.get("skill", "").split(":")[-1] == skill_name:
        return True
    needle = f"skills/{skill_name}/"
    return any(isinstance(v, str) and needle in v for v in cmd.parameters.values())


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

        triggered: bool = any(
            _engaged_skill(cmd, criterion.skill_name) for turn in turn_records for cmd in turn.commands
        )
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
