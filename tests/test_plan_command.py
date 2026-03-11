"""Tests for the plan command with experiment awareness."""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from coder_eval.cli.plan_command import plan_command
from coder_eval.models import AgentConfig, ExperimentDefinition, ExperimentVariant, TaskDefinition
from coder_eval.models.enums import AgentKind


# Since plan_command uses lazy imports from orchestration.experiment,
# patches must target the source module.
_EXP = "coder_eval.orchestration.experiment"


def _make_task(task_id: str = "test-task", agent: AgentConfig | None = None) -> TaskDefinition:
    """Create a minimal TaskDefinition for testing."""
    return TaskDefinition(
        task_id=task_id,
        description="A test task",
        initial_prompt="Do something",
        max_iterations=3,
        agent=agent,
        sandbox={"driver": "tempdir"},
        success_criteria=[{"type": "file_exists", "description": "check", "path": "out.txt"}],
    )


def _make_experiment(
    experiment_id: str = "test-exp",
    description: str = "Test experiment",
    variants: list[ExperimentVariant] | None = None,
) -> ExperimentDefinition:
    """Create a minimal ExperimentDefinition for testing."""
    if variants is None:
        variants = [
            ExperimentVariant(variant_id="sonnet", agent={"type": "claude-code", "model": "sonnet-4"}),
            ExperimentVariant(variant_id="opus", agent={"type": "claude-code", "model": "opus-4"}),
        ]
    return ExperimentDefinition(
        experiment_id=experiment_id,
        description=description,
        variants=variants,
    )


class TestPlanCommandAgentNone:
    """Tests for handling optional agent field."""

    def test_plan_shows_na_when_agent_is_none(self, tmp_path: Path) -> None:
        """When task.agent is None, plan should show 'N/A' instead of crashing."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved_task = _make_task(agent=AgentConfig(type=AgentKind.CLAUDE_CODE))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=resolved_task),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = [str(call) for call in mock_console.print.call_args_list]
        agent_lines = [p for p in printed if "Agent:" in p]
        assert len(agent_lines) == 1
        assert "N/A (resolved from experiment)" in agent_lines[0]

    def test_plan_shows_agent_type_when_present(self, tmp_path: Path) -> None:
        """When task.agent is set, plan should show the agent type."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        agent = AgentConfig(type=AgentKind.CLAUDE_CODE)
        task = _make_task(agent=agent)
        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])
        resolved_task = _make_task(agent=agent)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=resolved_task),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = [str(call) for call in mock_console.print.call_args_list]
        agent_lines = [p for p in printed if "Agent:" in p]
        assert len(agent_lines) == 1
        assert "claude-code" in agent_lines[0]


class TestPlanCommandExperiment:
    """Tests for experiment-aware plan output."""

    def test_plan_with_experiment_flag_shows_experiment_info(self, tmp_path: Path) -> None:
        """When --experiment is provided, plan should show experiment details."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()

        resolved_task = _make_task(agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=resolved_task),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", exp_file),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file], experiment=exp_file)

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "test-exp" in printed
        assert "Test experiment" in printed
        assert "sonnet" in printed
        assert "opus" in printed

    def test_plan_with_experiment_shows_resolved_agent_per_variant(self, tmp_path: Path) -> None:
        """When experiment is provided, plan should show resolved agent for each task x variant."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()

        resolved_sonnet = _make_task(agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))
        resolved_opus = _make_task(agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="opus-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", side_effect=[resolved_sonnet, resolved_opus]),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", exp_file),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file], experiment=exp_file)

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "sonnet-4" in printed
        assert "opus-4" in printed

    def test_plan_with_default_experiment(self, tmp_path: Path) -> None:
        """When default experiment file exists, plan auto-loads it."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        default_yaml = tmp_path / "default.yaml"
        default_yaml.write_text("placeholder")

        task = _make_task(agent=None)
        experiment = _make_experiment()
        resolved_task = _make_task(agent=AgentConfig(type=AgentKind.CLAUDE_CODE, model="sonnet-4"))

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", default_yaml),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch(f"{_EXP}.resolve_task_for_variant", return_value=resolved_task),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            plan_command(task_files=[task_file])

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "test-exp" in printed

    def test_plan_exits_when_default_experiment_missing(self, tmp_path: Path) -> None:
        """When default experiment file is missing and no --experiment given, plan should exit."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")

        agent = AgentConfig(type=AgentKind.CLAUDE_CODE)
        task = _make_task(agent=agent)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", tmp_path / "nonexistent.yaml"),
            patch(f"{_EXP}.load_experiment", side_effect=FileNotFoundError("not found")),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file])

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Failed to load experiment" in printed


class TestPlanCommandValidation:
    """Tests for plan command validation errors."""

    def test_plan_reports_invalid_task(self, tmp_path: Path) -> None:
        """Plan should report errors for invalid task files."""
        task_file = tmp_path / "bad_task.yaml"
        task_file.write_text("placeholder")

        experiment = _make_experiment(variants=[ExperimentVariant(variant_id="default")])

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", side_effect=ValueError("Bad schema")),
            patch(f"{_EXP}.load_experiment", return_value=experiment),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file])

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Bad schema" in printed

    def test_plan_exits_on_explicit_experiment_load_failure(self, tmp_path: Path) -> None:
        """When --experiment is explicitly provided and fails to load, plan should exit with code 1."""
        task_file = tmp_path / "task.yaml"
        task_file.write_text("placeholder")
        exp_file = tmp_path / "bad_experiment.yaml"
        exp_file.write_text("placeholder")

        task = _make_task(agent=None)

        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            patch("coder_eval.cli.plan_command.load_task", return_value=task),
            patch(f"{_EXP}.load_experiment", side_effect=ValueError("Bad experiment")),
            patch(f"{_EXP}.DEFAULT_EXPERIMENT_PATH", tmp_path / "nonexistent.yaml"),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            with pytest.raises(typer.Exit) as exc_info:
                plan_command(task_files=[task_file], experiment=exp_file)

            assert exc_info.value.exit_code == 1

        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "Bad experiment" in printed
