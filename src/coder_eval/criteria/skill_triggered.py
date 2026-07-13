"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic and detected purely by matching the recorded command text against
a skill-specific regex — never by inspecting the ``Skill`` tool identity, which
some harnesses lack. Two textual signals count as engagement:

* Claude Code engages a skill via a ``Skill`` tool call whose serialized argument
  is ``"skill": "<skill_name>"`` (optionally plugin-namespaced).
* Codex (and any harness without a ``Skill`` tool) auto-discovers skills under
  ``.agents/skills/`` and engages one by reading its ``SKILL.md`` / references off
  disk via shell — the command references ``skills/<skill_name>/``.

Both are matched as text, so the criterion scores identically across agents.
"""

from __future__ import annotations

import json
import logging
import re
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


def _engagement_pattern(skill_name: str) -> re.Pattern[str]:
    """Regex matching either textual signal that ``skill_name`` was engaged.

    * ``skills/<skill_name>/`` — the skill's files read off disk via shell/Read.
      Present in both the repo layout (``.../skills/<name>/...``) and the sandbox
      symlink (``.agents/skills/<name>/...``). The trailing slash prevents prefix
      collisions (``uipath-agents`` vs ``uipath-agents-foo``).
    * ``"skill": "<skill_name>"`` — a ``Skill`` tool call's serialized argument,
      optionally plugin-namespaced (e.g. ``"uipath:uipath-agents"``). The closing
      quote anchors the name so a namespaced value still matches but a longer skill
      id does not.

    Both alternatives are matched as text over the serialized parameters, so no
    ``Skill`` tool support is required.
    """
    esc = re.escape(skill_name)
    return re.compile(rf'skills/{esc}/|"skill"\s*:\s*"(?:[^"]*:)?{esc}"')


def _engaged_skill(cmd: CommandTelemetry, pattern: re.Pattern[str]) -> bool:
    """True when ``cmd``'s serialized parameters match the engagement ``pattern``."""
    return bool(pattern.search(json.dumps(cmd.parameters, default=str)))


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

        pattern = _engagement_pattern(criterion.skill_name)
        triggered: bool = any(_engaged_skill(cmd, pattern) for turn in turn_records for cmd in turn.commands)
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
