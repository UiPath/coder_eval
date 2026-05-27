"""Phase-3 regression: ``AgentConfig.type`` is optional; experiment / CLI must satisfy it."""

from __future__ import annotations

from pathlib import Path

import pytest

from coder_eval.models import (
    AgentKind,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.models.agent_config import parse_agent_config
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import _apply_cli_overrides, resolve_task_for_variant


def _empty_default_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}),
        variants=[ExperimentVariant(variant_id="default")],
    )


def _make_task(*, agent: dict | None = None) -> TaskDefinition:
    return TaskDefinition(
        task_id="optional_type",
        description="optional_type",
        initial_prompt="x",
        agent=parse_agent_config(**agent) if agent is not None else None,
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )


def test_agent_config_type_is_optional() -> None:
    cfg = parse_agent_config()
    assert cfg.type is None


def test_resolver_raises_when_type_missing_everywhere_no_config() -> None:
    """Direct call to resolve_task_for_variant (config=None) enforces type post-merge."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={}),  # no type
        variants=[ExperimentVariant(variant_id="default")],
    )
    task = _make_task(agent=None)
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )

    with pytest.raises(ValueError, match=r"Agent 'type' is required"):
        resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])


def test_resolver_raises_when_type_missing_after_cli_overrides() -> None:
    """Resolution + CLI overrides without --type still raises post-merge."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={}),  # no type
        variants=[ExperimentVariant(variant_id="default")],
    )
    task = _make_task(agent=None)
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )
    config = BatchRunConfig(run_dir=Path("."), agent_type=None)

    resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0], config)
    with pytest.raises(ValueError, match=r"Agent 'type' is required"):
        _apply_cli_overrides(resolved, config, lineage)


def test_cli_type_override_satisfies_contract() -> None:
    """`--type claude-code` (CLI) is enough when no other layer sets it."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={}),  # no type
        variants=[ExperimentVariant(variant_id="default")],
    )
    task = _make_task(agent=None)
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )
    config = BatchRunConfig(run_dir=Path("."), agent_type="claude-code")

    resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0], config)
    _apply_cli_overrides(resolved, config, lineage)
    assert resolved.agent is not None
    assert resolved.agent.type == AgentKind.CLAUDE_CODE
    assert lineage["agent.type"].source == "cli"


def test_task_with_only_model_resolves_when_type_supplied_via_default_experiment() -> None:
    """A task whose `agent:` block has only `model:` resolves cleanly when default experiment supplies type."""
    default_exp = _empty_default_experiment()
    task = _make_task(agent={"model": "claude-opus-4-7"})
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )

    resolved, _, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
    assert resolved.agent is not None
    assert resolved.agent.type == AgentKind.CLAUDE_CODE
    assert resolved.agent.model == "claude-opus-4-7"
