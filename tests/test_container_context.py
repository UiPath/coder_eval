"""`ContainerContext`: the host→container contract refuses every shape a skewed host/image pair produces."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coder_eval.models import ConfigLineageEntry, ContainerContext, PreservationMode
from tests._container_contract import contract_payload


def test_the_fixture_names_every_field() -> None:
    """Otherwise the missing-key parametrization below silently skips a new field."""
    assert set(contract_payload()) == set(ContainerContext.model_fields)


def test_every_field_is_required() -> None:
    """A default on any field restores the fallback a mismatched host/image pair used to hide behind."""
    defaulted = [name for name, field in ContainerContext.model_fields.items() if not field.is_required()]
    assert not defaulted, f"ContainerContext fields must not carry defaults: {defaulted}"


@pytest.mark.parametrize("field", sorted(ContainerContext.model_fields))
def test_a_missing_key_is_refused(field: str) -> None:
    with pytest.raises(ValidationError, match=rf"(?m)^{field}$"):
        ContainerContext.model_validate(contract_payload(omit=(field,)))


def test_an_unknown_key_is_refused() -> None:
    """A host newer than the image sends a key the image does not know. That must be a
    failure naming the key, never a silent ignore."""
    with pytest.raises(ValidationError, match="sent_by_a_newer_host"):
        ContainerContext.model_validate(contract_payload(sent_by_a_newer_host=1))


@pytest.mark.parametrize("field", ["grade", "regrade"])
@pytest.mark.parametrize("value", ["false", "False", "0", 0, 1])
def test_a_string_grade_is_refused(field: str, value: object) -> None:
    """Lax coercion is the defect: `"false"` is a truthy string, and a `regrade` read
    that way re-RUNS the agent over the workspace it was asked only to grade."""
    with pytest.raises(ValidationError, match=rf"(?m)^{field}$"):
        ContainerContext.model_validate(contract_payload(**{field: value}))


@pytest.mark.parametrize("value", [True, False, "00", 1.0])
def test_a_boolean_replicate_index_is_refused(value: object) -> None:
    """A bool is an int, so a lax field turns `True` into 1 and files the row under 01/."""
    with pytest.raises(ValidationError, match="replicate_index"):
        ContainerContext.model_validate(contract_payload(replicate_index=value))


def test_an_invalid_lineage_entry_names_config_lineage() -> None:
    with pytest.raises(ValidationError, match="config_lineage"):
        ContainerContext.model_validate(contract_payload(config_lineage={"agent.model": {"value": "m", "source": "?"}}))


def test_round_trip_through_json() -> None:
    ctx = ContainerContext(
        variant_id="v1",
        replicate_index=2,
        config_lineage={"agent.model": ConfigLineageEntry(value="claude-haiku-4-5", source="variant")},
        preservation_mode=PreservationMode.DIRECT_WRITE,
        grade=False,
        regrade=True,
        source_yaml="task_id: t\n",
        host_task_file="/host/tasks/t.yaml",
        workspace_dir="/root",
    )
    parsed = ContainerContext.model_validate_json(ctx.model_dump_json())
    assert parsed == ctx
    assert parsed.model_dump(mode="json") == ctx.model_dump(mode="json")


def test_host_task_file_null_is_accepted() -> None:
    """Required key, nullable value: `null` is a real answer (the task has no file); absence is not."""
    assert ContainerContext.model_validate(contract_payload(host_task_file=None)).host_task_file is None


def test_an_empty_config_lineage_is_accepted() -> None:
    assert ContainerContext.model_validate(contract_payload(config_lineage={})).config_lineage == {}
