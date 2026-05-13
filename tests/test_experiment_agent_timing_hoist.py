"""Tests for experiment-side `agent.max_turns` / `agent.turn_timeout` hoist.

Mirrors `test_task_definition_hoist_shim.py` but for `ExperimentDefaults.agent`
and `ExperimentVariant.agent` dicts (typed `dict[str, Any]`, bypass Pydantic
validation). Hoisted values land in `run_limits.*`. Scheduled removal: 2026-05-20.
"""

from __future__ import annotations

import warnings

from coder_eval.models import (
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.orchestration.experiment import resolve_task_for_variant


def _make_task() -> TaskDefinition:
    return TaskDefinition(
        task_id="hoist_test",
        description="hoist test",
        initial_prompt="do something",
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
    )


def _empty_default_experiment() -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}),
        variants=[ExperimentVariant(variant_id="default")],
    )


def test_default_experiment_agent_max_turns_hoisted() -> None:
    """Hoisted `default_experiment.defaults.agent.max_turns` lands in `resolved_task.run_limits.max_turns`."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code", "max_turns": 30}),
        variants=[ExperimentVariant(variant_id="default")],
    )
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        resolved, lineage, _ = resolve_task_for_variant(default_exp, _make_task(), experiment, experiment.variants[0])

    assert resolved.run_limits is not None
    assert resolved.run_limits.max_turns == 30
    assert lineage["run_limits.max_turns"].source == "default-agent-deprecated"
    deps = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert any("max_turns" in str(w.message) for w in deps)


def test_variant_agent_turn_timeout_hoisted() -> None:
    """Hoisted `variant.agent.turn_timeout` lands in `resolved_task.run_limits.turn_timeout`."""
    default_exp = _empty_default_experiment()
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1", agent={"turn_timeout": 60})],
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        resolved, lineage, _ = resolve_task_for_variant(default_exp, _make_task(), experiment, experiment.variants[0])

    assert resolved.run_limits is not None
    assert resolved.run_limits.turn_timeout == 60
    assert lineage["run_limits.turn_timeout"].source == "variant-agent-deprecated"
    deps = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert any("turn_timeout" in str(w.message) for w in deps)


def test_run_limits_wins_over_agent_hoisted_within_same_layer() -> None:
    """When both `defaults.run_limits.max_turns` and `defaults.agent.max_turns` are set, run_limits wins."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(
            agent={"type": "claude-code", "max_turns": 99},
            run_limits=RunLimits(max_turns=20),
        ),
        variants=[ExperimentVariant(variant_id="default")],
    )
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        resolved, lineage, _ = resolve_task_for_variant(default_exp, _make_task(), experiment, experiment.variants[0])

    assert resolved.run_limits is not None
    assert resolved.run_limits.max_turns == 20
    assert lineage["run_limits.max_turns"].source == "default"


def test_variant_run_limits_wins_over_variant_agent_hoisted() -> None:
    """Variant `run_limits.max_turns` always wins over variant `agent.max_turns` in the same layer."""
    default_exp = _empty_default_experiment()
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[
            ExperimentVariant(
                variant_id="v1",
                run_limits=RunLimits(max_turns=15),
                agent={"max_turns": 999},
            )
        ],
    )

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        resolved, lineage, _ = resolve_task_for_variant(default_exp, _make_task(), experiment, experiment.variants[0])

    assert resolved.run_limits is not None
    assert resolved.run_limits.max_turns == 15
    assert lineage["run_limits.max_turns"].source == "variant"


def test_4_layer_precedence_max_turns() -> None:
    """default < experiment-defaults < task < variant for `run_limits.max_turns`."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code"}, run_limits=RunLimits(max_turns=10)),
        variants=[ExperimentVariant(variant_id="default")],
    )
    task = TaskDefinition(
        task_id="layered",
        description="layered",
        initial_prompt="x",
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(type="file_exists", path="x.py", description="x.py exists")],
        run_limits=RunLimits(max_turns=30),
    )
    experiment = ExperimentDefinition(
        experiment_id="test",
        defaults=ExperimentDefaults(run_limits=RunLimits(max_turns=20)),
        variants=[ExperimentVariant(variant_id="v1", run_limits=RunLimits(max_turns=40))],
    )

    resolved, lineage, _ = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])

    assert resolved.run_limits is not None
    assert resolved.run_limits.max_turns == 40
    assert lineage["run_limits.max_turns"].source == "variant"


def test_no_legacy_shape_anywhere_emits_no_warning() -> None:
    """Pure run_limits-based resolution emits no DeprecationWarning."""
    default_exp = ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(
            agent={"type": "claude-code"},
            run_limits=RunLimits(max_turns=20, turn_timeout=300),
        ),
        variants=[ExperimentVariant(variant_id="default")],
    )
    experiment = ExperimentDefinition(
        experiment_id="test",
        variants=[ExperimentVariant(variant_id="v1")],
    )

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        resolve_task_for_variant(default_exp, _make_task(), experiment, experiment.variants[0])

    deps = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deps == []
