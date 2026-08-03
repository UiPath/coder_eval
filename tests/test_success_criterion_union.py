"""Tests for the SuccessCriterion discriminated union.

Pins the Annotated[..., Field(discriminator="type")] contract: a missing or
typo'd ``type`` tag in dict/YAML input raises one crisp discriminator error
(not a per-variant wall), while direct construction and model_dump round-trips
keep working via the per-variant Literal defaults.
"""

from typing import get_args

import pytest
from pydantic import ValidationError

from coder_eval.criteria import CriterionRegistry, validate_registry
from coder_eval.models import FileExistsCriterion, SuccessCriterion, TaskDefinition


def _make_task(criteria: list[dict]) -> TaskDefinition:
    return TaskDefinition.model_validate(
        {
            "task_id": "t",
            "description": "d",
            "initial_prompt": "do the thing",
            "success_criteria": criteria,
        }
    )


# One minimal valid payload per discriminator tag. The parity assert in
# test_all_variant_tags_validate forces this dict to grow with the union.
MINIMAL_PAYLOADS: dict[str, dict] = {
    "file_exists": {"description": "d", "path": "f.txt"},
    "file_contains": {"description": "d", "path": "f.txt", "includes": ["x"]},
    "run_command": {"description": "d", "command": "true"},
    "file_matches_regex": {"description": "d", "path": "f.txt", "pattern": "x"},
    "file_check": {"description": "d", "path": "f.txt"},
    "json_check": {"description": "d", "path": "f.json"},
    "reference_comparison": {"description": "d", "agent_file": "f.py"},
    "command_executed": {"description": "d"},
    "cli_called": {"description": "d", "log": "calls.jsonl", "verb": "ixp projects get"},
    "commands_efficiency": {"description": "d", "expected_commands": 3},
    "uipath_eval": {"description": "d", "agent_name": "a", "eval_set": "e", "thresholds": {"accuracy": 0.8}},
    "classification_match": {
        "description": "d",
        "path": "f.txt",
        "expected_label": "positive",
        "allowed_labels": ["positive", "negative"],
    },
    "skill_triggered": {"description": "d", "expected_skill": "s", "skill_name": "s"},
    "llm_judge": {"description": "d", "prompt": "grade it"},
    "agent_judge": {"description": "d", "prompt": "grade it"},
}


def test_missing_type_tag_fails_with_discriminator_error():
    with pytest.raises(ValidationError) as exc:
        _make_task([{"description": "d", "path": "f.txt"}])
    assert "discriminator" in str(exc.value).lower()
    # One crisp error, not a 14-variant wall
    assert len(exc.value.errors()) <= 2


def test_typo_type_tag_fails_naming_valid_tags():
    with pytest.raises(ValidationError) as exc:
        _make_task([{"type": "file_exist", "description": "d", "path": "f.txt"}])
    msg = str(exc.value)
    assert "file_exist" in msg
    assert "file_exists" in msg  # the valid-tags set is named in the error


@pytest.mark.parametrize("tag", sorted(MINIMAL_PAYLOADS))
def test_all_variant_tags_validate(tag: str):
    task = _make_task([{"type": tag, **MINIMAL_PAYLOADS[tag]}])
    assert task.success_criteria[0].type == tag


def test_minimal_payloads_cover_every_union_member():
    inner = get_args(SuccessCriterion)[0]
    union_tags = {m.model_fields["type"].default for m in get_args(inner)}
    assert set(MINIMAL_PAYLOADS) == union_tags


def test_direct_construction_without_type_kwarg():
    criterion = FileExistsCriterion(description="d", path="p")
    assert criterion.type == "file_exists"


def test_model_dump_round_trip():
    task = _make_task(
        [
            {"type": "file_exists", "description": "d", "path": "f.txt"},
            {"type": "run_command", "description": "d", "command": "true"},
            {"type": "llm_judge", "description": "d", "prompt": "grade it"},
        ]
    )
    round_tripped = TaskDefinition.model_validate(task.model_dump())
    assert [c.type for c in round_tripped.success_criteria] == ["file_exists", "run_command", "llm_judge"]


def test_model_dump_exclude_unset_round_trip():
    """The discriminator tag must survive exclude_unset dumps of directly-constructed criteria.

    Direct construction supplies the tag via the Literal default, which would
    normally be absent from model_fields_set — and exclude_unset would drop it,
    breaking re-validation through the discriminated union.
    """
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="do the thing",
        success_criteria=[FileExistsCriterion(description="d", path="f.txt")],
    )
    round_tripped = TaskDefinition.model_validate(task.model_dump(exclude_unset=True))
    assert round_tripped.success_criteria[0].type == "file_exists"


def test_validate_registry_passes():
    CriterionRegistry.discover()
    validate_registry()


def test_validate_registry_raises_on_missing_checker():
    CriterionRegistry.discover()
    removed = CriterionRegistry._checkers.pop("file_exists")
    try:
        with pytest.raises(RuntimeError, match="file_exists"):
            validate_registry()
    finally:
        CriterionRegistry._checkers["file_exists"] = removed
