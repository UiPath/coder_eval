"""Tests for experiment data models."""

from datetime import datetime

import pytest

from coder_eval.models import (
    EvaluationResult,
    ExperimentBase,
    ExperimentDefinition,
    ExperimentVariant,
    ResolvedTask,
    TaskDefinition,
    TaskResult,
)


class TestExperimentVariant:
    def test_minimal_variant(self):
        variant = ExperimentVariant(variant_id="sonnet")
        assert variant.variant_id == "sonnet"
        assert variant.agent is None
        assert variant.max_iterations is None

    def test_variant_with_agent_overrides(self):
        variant = ExperimentVariant(variant_id="opus", agent={"model": "claude-opus-4-20250514"})
        assert variant.agent == {"model": "claude-opus-4-20250514"}

    def test_variant_with_all_fields(self):
        variant = ExperimentVariant(
            variant_id="fast",
            agent={"model": "claude-sonnet-4-20250514", "max_turns": 5},
            max_iterations=2,
            task_timeout=120,
            turn_timeout=30,
        )
        assert variant.max_iterations == 2
        assert variant.task_timeout == 120


class TestExperimentBase:
    def test_empty_base(self):
        base = ExperimentBase()
        assert base.max_iterations is None
        assert base.agent is None

    def test_base_with_all_fields(self):
        base = ExperimentBase(
            max_iterations=3,
            task_timeout=300,
            turn_timeout=120,
            agent={"permission_mode": "bypassPermissions"},
        )
        assert base.max_iterations == 3
        assert base.agent == {"permission_mode": "bypassPermissions"}


class TestExperimentDefinition:
    def test_minimal_experiment(self):
        exp = ExperimentDefinition(
            experiment_id="default",
            variants=[ExperimentVariant(variant_id="default")],
        )
        assert exp.experiment_id == "default"
        assert exp.description == ""
        assert exp.base is None
        assert len(exp.variants) == 1

    def test_full_experiment(self):
        exp = ExperimentDefinition(
            experiment_id="model-comparison",
            description="Compare Sonnet vs Opus",
            base=ExperimentBase(max_iterations=3, agent={"permission_mode": "bypassPermissions"}),
            variants=[
                ExperimentVariant(variant_id="sonnet", agent={"model": "claude-sonnet-4-20250514"}),
                ExperimentVariant(variant_id="opus", agent={"model": "claude-opus-4-20250514"}),
            ],
        )
        assert len(exp.variants) == 2
        assert exp.base.max_iterations == 3

    def test_no_variants_raises(self):
        with pytest.raises(ValueError, match="at least 1"):
            ExperimentDefinition(experiment_id="empty", variants=[])

    def test_duplicate_variant_ids_raises(self):
        with pytest.raises(ValueError, match="unique"):
            ExperimentDefinition(
                experiment_id="dup",
                variants=[ExperimentVariant(variant_id="a"), ExperimentVariant(variant_id="a")],
            )

    def test_experiment_id_kebab_case(self):
        with pytest.raises(ValueError, match="kebab-case"):
            ExperimentDefinition(experiment_id="Bad Name!", variants=[ExperimentVariant(variant_id="x")])


class TestTaskDefinitionOptionalAgent:
    def test_task_without_agent(self):
        """TaskDefinition should accept agent=None."""
        task = TaskDefinition(
            task_id="no-agent-task",
            description="A task without an agent section",
            initial_prompt="Do something",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "test.py", "description": "File exists"}],
        )
        assert task.agent is None

    def test_task_with_agent_still_works(self):
        """Existing tasks with agent defined should still work."""
        task = TaskDefinition(
            task_id="agent-task",
            description="A task with agent",
            initial_prompt="Do something",
            agent={"type": "claude-code"},
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "test.py", "description": "File exists"}],
        )
        assert task.agent is not None
        assert task.agent.type == "claude-code"


class TestResolvedTask:
    def test_creates_with_required_fields(self, tmp_path):
        task = TaskDefinition(
            task_id="t1",
            description="d",
            initial_prompt="p",
            sandbox={"driver": "tempdir"},
            success_criteria=[{"type": "file_exists", "path": "f.py", "description": "d"}],
        )
        rt = ResolvedTask(
            task=task,
            task_file=tmp_path / "t1.yaml",
            run_dir=tmp_path / "runs" / "t1",
            variant_id="default",
        )
        assert rt.task.task_id == "t1"
        assert rt.variant_id == "default"
        assert rt.run_dir == tmp_path / "runs" / "t1"


class TestTaskResult:
    def test_creates_with_required_fields(self):
        er = EvaluationResult(
            task_id="t1",
            task_description="d",
            variant_id="test-variant",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status="SUCCESS",
            iteration_count=1,
            environment_info={},
        )
        tr = TaskResult(task_id="t1", variant_id="test-variant", result=er, duration=1.0)
        assert tr.task_id == "t1"
        assert tr.duration == 1.0
        assert tr.result.final_status == "SUCCESS"
