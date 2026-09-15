"""`ContainerContext`: the host→container contract refuses every shape a skewed host/image pair produces."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.models import (
    AgentKind,
    ConfigLineageEntry,
    ContainerContext,
    EvaluationResult,
    FinalStatus,
    PreservationMode,
)
from coder_eval.path_utils import PRIOR_RESULT_FILENAME, TASK_JSON_FILENAME
from coder_eval.reports import is_env_table_key
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


# --------------------------------------------------------------------------
# The echo, driven end to end through the real container entry point
# --------------------------------------------------------------------------

_AGENTLESS_TASK_YAML = (
    "task_id: echo\ndescription: d\nagent:\n  type: none\n"
    "success_criteria:\n  - type: file_exists\n    path: out.txt\n    description: d\n"
)


def _run_container_entry_point(tmp_path: Path, **overrides: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Invoke `_run-task-internal` in process; return (the staged payload, the echo task.json carries)."""
    input_dir = tmp_path / "input"
    input_dir.mkdir(exist_ok=True)
    (input_dir / "task.yaml").write_text(_AGENTLESS_TASK_YAML, encoding="utf-8")
    payload = contract_payload(source_yaml=_AGENTLESS_TASK_YAML, **overrides)
    (input_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")
    output_dir = tmp_path / "out"

    invoked = CliRunner().invoke(app, ["_run-task-internal", "--input", str(input_dir), "--output", str(output_dir)])

    record = output_dir / TASK_JSON_FILENAME
    assert record.is_file(), invoked.output
    written = EvaluationResult.model_validate_json(record.read_text(encoding="utf-8"))
    return payload, written.environment_info["container_contract"]


def test_every_contract_field_is_echoed(tmp_path: Path) -> None:
    """Derived from `model_fields`, so a field added to the contract is checked with no new test."""
    payload, echo = _run_container_entry_point(tmp_path, grade=False)

    assert set(echo) == set(ContainerContext.model_fields)
    assert echo == ContainerContext.model_validate(payload).model_dump(mode="json")


def test_the_echo_survives_a_regrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_seed_from_prior_result` lets the PRIOR row's environment_info win. The prior here
    carries a stale echo from an earlier pass, so an echo written before the seed would be
    replaced by it and the host would refuse a correct grade."""
    from coder_eval import models

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out.txt").write_text("done", encoding="utf-8")
    monkeypatch.setattr(models, "CONTAINER_GRADE_WORKSPACE", str(workspace))

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    prior = EvaluationResult(
        task_id="echo",
        task_description="d",
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=0,
        environment_info={"container_contract": {"stale": "from an earlier pass"}},
    )
    (input_dir / PRIOR_RESULT_FILENAME).write_text(prior.model_dump_json(), encoding="utf-8")

    payload, echo = _run_container_entry_point(tmp_path, regrade=True)

    assert echo == ContainerContext.model_validate(payload).model_dump(mode="json")


async def test_a_host_grade_drops_the_prior_echo(tmp_path: Path) -> None:
    """A host grade is not a container's verdict. Keeping the prior echo would record
    `grade: false` / `regrade: false` on a row that this pass just graded."""
    from coder_eval.orchestration.regrade import regrade_in_place
    from coder_eval.orchestration.task_loader import load_task

    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(_AGENTLESS_TASK_YAML, encoding="utf-8")
    task, source_yaml = load_task(task_yaml)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out.txt").write_text("done", encoding="utf-8")
    prior = EvaluationResult(
        task_id="echo",
        task_description="d",
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=0,
        environment_info={
            "container_contract": ContainerContext.model_validate(contract_payload(grade=False)).model_dump(mode="json")
        },
    )

    graded = await regrade_in_place(
        task=task,
        prior=prior,
        workspace=workspace,
        run_dir=tmp_path / "grade",
        task_file=task_yaml,
        source_yaml=source_yaml,
        variant_id="default",
    )

    assert graded.final_status is FinalStatus.SUCCESS
    assert "container_contract" not in graded.environment_info


def test_the_echo_is_not_rendered_in_the_environment_table() -> None:
    """A nested object in a flat key/value table renders as a Python dict repr."""
    assert not is_env_table_key("container_contract")
