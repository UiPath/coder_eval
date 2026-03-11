"""Tests for experiment config resolution (merge logic)."""

from coder_eval.models import ExperimentBase, ExperimentDefinition, ExperimentVariant, TaskDefinition
from coder_eval.orchestration.experiment import resolve_task_for_variant


def _make_task(agent: dict | None = None, **kwargs) -> TaskDefinition:
    """Create a minimal TaskDefinition."""
    defaults = {
        "task_id": "test-task",
        "description": "Test task",
        "initial_prompt": "Do something",
        "sandbox": {"driver": "tempdir"},
        "success_criteria": [{"type": "file_exists", "path": "test.py", "description": "File exists"}],
    }
    if agent is not None:
        defaults["agent"] = agent
    defaults.update(kwargs)
    return TaskDefinition(**defaults)


def _make_default_experiment() -> ExperimentDefinition:
    """Create the default experiment (mimics experiments/default.yaml)."""
    return ExperimentDefinition(
        experiment_id="default",
        base=ExperimentBase(agent={"type": "claude-code", "permission_mode": "acceptEdits"}),
        variants=[ExperimentVariant(variant_id="default")],
    )


class TestResolveTaskForVariant:
    def test_default_fills_missing_agent(self):
        """Task without agent gets defaults from default experiment."""
        default_exp = _make_default_experiment()
        task = _make_task(agent=None)
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent is not None
        assert resolved.agent.type == "claude-code"
        assert resolved.agent.permission_mode == "acceptEdits"

    def test_task_agent_overrides_default(self):
        """Task agent fields override default experiment fields."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "permission_mode": "bypassPermissions"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.permission_mode == "bypassPermissions"

    def test_experiment_base_overrides_task(self):
        """Experiment base overrides task agent settings."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "permission_mode": "acceptEdits"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(agent={"permission_mode": "bypassPermissions"}),
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.permission_mode == "bypassPermissions"

    def test_variant_overrides_base(self):
        """Variant settings override experiment base."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"})
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(agent={"model": "base-model"}),
            variants=[ExperimentVariant(variant_id="variant1", agent={"model": "variant-model"})],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.model == "variant-model"

    def test_full_precedence_chain(self):
        """Full 4-layer merge: default < task < base < variant."""
        default_exp = ExperimentDefinition(
            experiment_id="default",
            base=ExperimentBase(
                agent={
                    "type": "claude-code",
                    "permission_mode": "acceptEdits",
                    "allowed_tools": ["Read"],
                }
            ),
            variants=[ExperimentVariant(variant_id="default")],
        )
        task = _make_task(
            agent={
                "type": "claude-code",
                "allowed_tools": ["Read", "Write", "Bash"],
            }
        )
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(agent={"permission_mode": "bypassPermissions"}),
            variants=[ExperimentVariant(variant_id="opus", agent={"model": "claude-opus-4-20250514"})],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.type == "claude-code"
        assert resolved.agent.allowed_tools == ["Read", "Write", "Bash"]  # from task (layer 2)
        assert resolved.agent.permission_mode == "bypassPermissions"  # from base (layer 3)
        assert resolved.agent.model == "claude-opus-4-20250514"  # from variant (layer 4)

    def test_list_fields_replace_atomically(self):
        """List fields (allowed_tools) in later layers fully replace earlier values."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code", "allowed_tools": ["Read", "Write", "Bash"]})
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="limited", agent={"allowed_tools": ["Read", "Bash"]})],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.agent.allowed_tools == ["Read", "Bash"]

    def test_scalar_overrides(self):
        """Scalar fields (max_iterations, task_timeout) resolve through precedence."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, max_iterations=3)
        experiment = ExperimentDefinition(
            experiment_id="test",
            base=ExperimentBase(max_iterations=5, task_timeout=300),
            variants=[ExperimentVariant(variant_id="fast", max_iterations=2, task_timeout=120)],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.max_iterations == 2  # variant wins
        assert resolved.task_timeout == 120  # variant wins

    def test_resolved_task_preserves_non_agent_fields(self):
        """Resolution should not alter task_id, description, criteria, sandbox, etc."""
        default_exp = _make_default_experiment()
        task = _make_task(agent={"type": "claude-code"}, task_id="my-task", description="My test")
        experiment = ExperimentDefinition(
            experiment_id="test",
            variants=[ExperimentVariant(variant_id="variant1")],
        )

        resolved = resolve_task_for_variant(default_exp, task, experiment, experiment.variants[0])
        assert resolved.task_id == "my-task"
        assert resolved.description == "My test"
        assert len(resolved.success_criteria) == 1
