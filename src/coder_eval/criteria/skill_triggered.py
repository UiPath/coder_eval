"""Skill-triggered criterion checker: did the agent engage the target skill?

Agent-agnostic. Every harness receives the staged ``skills/<name>/`` layout. Claude
Code engages a skill via an explicit ``Skill`` tool call; a harness with no such tool
engages one by reading its ``SKILL.md`` / references off disk via shell. Both signals
are detected here so the criterion scores identically across agents.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from coder_eval.criteria._classification_aggregate import overlay_classification_metrics
from coder_eval.criteria.base import BaseCriterion, LiveVerdict, register_criterion
from coder_eval.errors import CheckerMisuseError
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

# Both separator styles, because telemetry records the command exactly as the agent
# emitted it; one-or-more also handles JSON-escaped doubled backslashes. The
# lookahead permits overlapping matches like ``.../skills/skills/<name>/...``.
_SKILL_PATH_RE = re.compile(r"(?=skills[\\/]+([A-Za-z0-9][A-Za-z0-9_-]*)[\\/]+)")


def _engaged_skill_names(cmd: CommandTelemetry) -> set[str]:
    """All skill names engaged by ONE command, agent-agnostically (any-skill).

    Detects both engagement signals so the criterion scores identically across
    agents: Claude's explicit ``Skill`` tool call (namespace stripped), and, for
    every other agent, the ``skills/<name>/`` substring in any string parameter.

    Returning the full SET rather than a single-skill yes/no lets callers detect a
    *competing* skill engagement.

    Rationale: .claude/notes/contracts.md § Any-engagement, and why order does not matter
    """
    names: set[str] = set()
    if cmd.tool_name == "Skill":
        skill = cmd.parameters.get("skill") or ""
        if isinstance(skill, str) and skill:
            names.add(skill.split(":")[-1])
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

        offered = context.skills_offered if context is not None else None
        if offered is not None and criterion.skill_name not in offered:
            raise CheckerMisuseError(
                f"skill_triggered names skill {criterion.skill_name!r} but agent.plugins offered only "
                + f"{sorted(offered)}: the positive control cannot run. Check the plugin path (both "
                + "`<root>/skills/<name>/SKILL.md` and `<root>/<name>/SKILL.md` are accepted)."
            )

        # Any-engagement policy, mirroring ``live_verdict``: scored on whether this
        # skill was engaged AT ALL, regardless of order.
        # Rationale: .claude/notes/contracts.md § Any-engagement, and why order does not matter
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

        Monotonic and deterministic, as the ``LiveVerdict`` contract requires: a
        decided verdict is a latch over a prefix that only grows.

        A positive criterion can therefore only ever live-``pass`` and a distractor
        only ever live-``fail``; the ABSENCE of an engagement is never decidable
        mid-run (see ``live_decidable_polarities``).

        Rationale: .claude/notes/contracts.md § Any-engagement, and why order does not matter
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
