"""Unit tests for SimulationConfig and its integration on TaskDefinition."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from coder_eval.models import (
    DEFAULT_SIMULATION_STOP_TOKEN,
    AgentKind,
    FileExistsCriterion,
    SandboxConfig,
    SimulationConfig,
    TaskDefinition,
    parse_agent_config,
)


def _minimal_sim_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"persona": "A business analyst.", "goal": "Build an invoice pipeline."}
    base.update(overrides)
    return base


class TestSimulationConfigDefaults:
    def test_disabled_by_default(self):
        cfg = SimulationConfig(**_minimal_sim_kwargs())
        assert cfg.enabled is False

    def test_sensible_defaults(self):
        cfg = SimulationConfig(**_minimal_sim_kwargs())
        assert cfg.max_turns == 8
        assert cfg.stop_token == DEFAULT_SIMULATION_STOP_TOKEN
        assert cfg.n_trials == 1
        assert cfg.check_criteria == "end_of_dialog"
        # Default stop_on_criteria_pass=False so that the default
        # check_criteria=end_of_dialog pairing is internally consistent.
        assert cfg.stop_on_criteria_pass is False


class TestSimulationConfigValidators:
    def test_blank_persona_rejected(self):
        with pytest.raises(ValidationError) as exc:
            SimulationConfig(**_minimal_sim_kwargs(persona="   "))
        assert "non-empty" in str(exc.value)

    def test_blank_goal_rejected(self):
        with pytest.raises(ValidationError) as exc:
            SimulationConfig(**_minimal_sim_kwargs(goal=""))
        assert "non-empty" in str(exc.value)

    def test_stop_token_non_empty(self):
        with pytest.raises(ValidationError):
            SimulationConfig(**_minimal_sim_kwargs(stop_token=""))

    def test_max_turns_bounds(self):
        with pytest.raises(ValidationError):
            SimulationConfig(**_minimal_sim_kwargs(max_turns=0))
        with pytest.raises(ValidationError):
            SimulationConfig(**_minimal_sim_kwargs(max_turns=101))

    def test_n_trials_bounds(self):
        with pytest.raises(ValidationError):
            SimulationConfig(**_minimal_sim_kwargs(n_trials=0))

    def test_stop_on_pass_requires_every_turn(self):
        """stop_on_criteria_pass=True + check_criteria='end_of_dialog' is incoherent."""
        with pytest.raises(ValidationError) as exc:
            SimulationConfig(
                **_minimal_sim_kwargs(
                    stop_on_criteria_pass=True,
                    check_criteria="end_of_dialog",
                )
            )
        assert "stop_on_criteria_pass" in str(exc.value)

    def test_stop_on_pass_ok_with_every_turn(self):
        cfg = SimulationConfig(
            **_minimal_sim_kwargs(
                stop_on_criteria_pass=True,
                check_criteria="every_turn",
            )
        )
        assert cfg.check_criteria == "every_turn"

    def test_stop_on_pass_ok_when_disabled(self):
        cfg = SimulationConfig(
            **_minimal_sim_kwargs(
                stop_on_criteria_pass=False,
                check_criteria="end_of_dialog",
            )
        )
        assert cfg.stop_on_criteria_pass is False

    def test_forbid_extra_fields(self):
        with pytest.raises(ValidationError):
            SimulationConfig(**_minimal_sim_kwargs(unknown_field="oops"))


class TestSimulationOnTaskDefinition:
    def _task(self, **overrides: Any) -> TaskDefinition:
        kwargs: dict[str, Any] = {
            "task_id": "sim-task",
            "description": "A simulated task",
            "initial_prompt": "Hello, I need help.",
            "agent": parse_agent_config(type=AgentKind.CLAUDE_CODE),
            "sandbox": SandboxConfig(driver="tempdir"),
            "success_criteria": [FileExistsCriterion(path="out.txt", description="file exists")],
        }
        kwargs.update(overrides)
        return TaskDefinition(**kwargs)

    def test_simulation_omitted(self):
        task = self._task()
        assert task.simulation is None

    def test_simulation_attached(self):
        sim = SimulationConfig(
            **_minimal_sim_kwargs(
                enabled=True,
                stop_on_criteria_pass=False,
            )
        )
        task = self._task(simulation=sim)
        assert task.simulation is not None
        assert task.simulation.enabled is True
        assert task.simulation.goal == "Build an invoice pipeline."

    def test_simulation_from_dict(self):
        task = self._task(
            simulation={
                "enabled": True,
                "persona": "BA user",
                "goal": "Ship a flow",
                "stop_on_criteria_pass": False,
            }
        )
        assert isinstance(task.simulation, SimulationConfig)
        assert task.simulation.persona == "BA user"

    def test_initial_prompt_optional_when_simulation_enabled(self):
        """Pure-simulation tasks may omit initial_prompt — simulator generates the opener."""
        sim = SimulationConfig(**_minimal_sim_kwargs(enabled=True))
        task = self._task(initial_prompt=None, simulation=sim)
        assert task.initial_prompt is None
        assert task.simulation is not None

    def test_initial_prompt_still_required_when_simulation_disabled(self):
        """simulation.enabled=False leaves the original prompt contract intact."""
        sim = SimulationConfig(**_minimal_sim_kwargs(enabled=False))
        with pytest.raises(ValidationError, match="initial_prompt"):
            self._task(initial_prompt=None, simulation=sim)

    def test_initial_prompt_still_required_when_no_simulation_block(self):
        with pytest.raises(ValidationError, match="initial_prompt"):
            self._task(initial_prompt=None)
