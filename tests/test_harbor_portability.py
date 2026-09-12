"""``coder_eval.harbor.portability`` — the C1.4 criteria portability audit.

Registry-derived coverage (the same shape CE036 uses for
``live_decidable_polarities``): every criterion type in the real
``SuccessCriterion`` union must have a portability classification, so a 16th
criterion type added later fails this test instead of silently exporting as
if it were ``PORTABLE``.
"""

from __future__ import annotations

import typing

import pytest

from coder_eval.harbor.portability import (
    _PORTABILITY_BY_TYPE,
    CriterionPortability,
    UnknownCriterionTypeError,
    audit_criteria,
    classify,
)
from coder_eval.models import SuccessCriterion
from coder_eval.models.criteria import (
    AgentJudgeCriterion,
    ClassificationMatchCriterion,
    CliCalledCriterion,
    CommandExecutedCriterion,
    CommandsEfficiencyCriterion,
    FileCheckCriterion,
    FileContainsCriterion,
    FileExistsCriterion,
    FileMatchesRegexCriterion,
    JsonCheckCriterion,
    LLMJudgeCriterion,
    ReferenceComparisonCriterion,
    RunCommandCriterion,
    SkillTriggeredCriterion,
    UiPathEvalCriterion,
)


def _union_member_type_tags() -> set[str]:
    """Every ``type:`` literal tag actually reachable through the real union."""
    # SuccessCriterion is `Annotated[X | Y | ..., Field(discriminator="type")]`.
    union = typing.get_args(SuccessCriterion)[0]
    tags: set[str] = set()
    for member in typing.get_args(union):
        default = member.model_fields["type"].default
        assert isinstance(default, str)
        tags.add(default)
    return tags


def test_every_real_criterion_type_is_classified() -> None:
    """Fails closed: a new criterion type added to the union with no line here is a bug, not a PORTABLE default."""
    assert _union_member_type_tags() == set(_PORTABILITY_BY_TYPE)


def test_unknown_type_raises_rather_than_defaulting_portable() -> None:
    with pytest.raises(UnknownCriterionTypeError):
        classify("not_a_real_criterion_type")


@pytest.mark.parametrize(
    "criterion",
    [
        FileExistsCriterion(description="d", path="p"),
        FileContainsCriterion(description="d", path="p", includes=["t"]),
        FileMatchesRegexCriterion(description="d", path="p", pattern="."),
        JsonCheckCriterion(description="d", path="p"),
        FileCheckCriterion(description="d", path="p"),
        RunCommandCriterion(description="d", command="true"),
        ClassificationMatchCriterion(description="d", path="p", expected_label="x", allowed_labels=["x", "y"]),
    ],
)
def test_portable_criteria_never_block_export(criterion: object) -> None:
    assert audit_criteria([criterion]) == []  # type: ignore[list-item]


def test_reference_comparison_does_not_block_export() -> None:
    """NEEDS_REFERENCE is not in _BLOCKING_IN_V1 — C2 always emits tests/reference/."""
    criterion = ReferenceComparisonCriterion(description="d", agent_file="p", reference_file="r")
    assert audit_criteria([criterion]) == []


@pytest.mark.parametrize(
    "criterion",
    [
        CommandExecutedCriterion(description="d", command_pattern="."),
        CommandsEfficiencyCriterion(description="d", expected_commands=3),
        SkillTriggeredCriterion(description="d", expected_skill="s", skill_name="s"),
        CliCalledCriterion(description="d", verb="v"),
    ],
)
def test_missing_functionality_criteria_always_block_export(criterion: object) -> None:
    """No flag can supply what does not exist yet (C1.3 / a recorder-baking step)."""
    issues = audit_criteria([criterion], allow_credentials=True)  # type: ignore[list-item]
    assert len(issues) == 1
    assert issues[0].criterion_type == criterion.type  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "criterion",
    [
        LLMJudgeCriterion(description="d", prompt="p"),
        AgentJudgeCriterion(description="d", prompt="p"),
        UiPathEvalCriterion(description="d", agent_name="a", eval_set="p", thresholds={}),
    ],
)
def test_credentials_criteria_block_by_default_but_have_an_escape_hatch(criterion: object) -> None:
    assert len(audit_criteria([criterion])) == 1  # type: ignore[list-item]
    assert audit_criteria([criterion], allow_credentials=True) == []  # type: ignore[list-item]


def test_a_clean_multi_criterion_task_reports_no_issues() -> None:
    criteria = [
        FileExistsCriterion(description="d1", path="p1"),
        ReferenceComparisonCriterion(description="d2", agent_file="p2", reference_file="r"),
    ]
    assert audit_criteria(criteria) == []


def test_a_mixed_task_reports_only_the_blocking_criteria() -> None:
    criteria = [
        FileExistsCriterion(description="portable", path="p1"),
        SkillTriggeredCriterion(description="needs trajectory", expected_skill="s", skill_name="s"),
        LLMJudgeCriterion(description="needs credentials", prompt="p"),
    ]
    issues = audit_criteria(criteria)
    assert {i.criterion_description for i in issues} == {"needs trajectory", "needs credentials"}
    assert {i.portability for i in issues} == {
        CriterionPortability.NEEDS_TRAJECTORY,
        CriterionPortability.NEEDS_CREDENTIALS,
    }
