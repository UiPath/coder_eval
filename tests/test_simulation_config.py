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


def _scripted_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"enabled": True, "turns": ["First question.", "Second question."]}
    base.update(overrides)
    return base


class TestScriptedTurns:
    """``turns`` scripts the user, so the simulator's knobs have nothing to do and must not be set."""

    def test_persona_and_goal_are_not_needed_to_script_the_user(self):
        cfg = SimulationConfig(**_scripted_kwargs())
        assert cfg.is_scripted is True
        assert cfg.turns == ["First question.", "Second question."]
        assert cfg.persona is None
        assert cfg.goal is None

    def test_a_simulated_user_is_not_scripted(self):
        assert SimulationConfig(**_minimal_sim_kwargs()).is_scripted is False

    def test_a_simulated_user_still_needs_persona_and_goal(self):
        with pytest.raises(ValidationError, match=r"persona.*goal"):
            SimulationConfig(enabled=True)
        with pytest.raises(ValidationError, match=r"persona.*goal"):
            SimulationConfig(enabled=True, persona="A user")

    def test_turns_are_kept_verbatim(self):
        turns = ["  leading and trailing space\n", "Line one\n\nLine two"]
        assert SimulationConfig(**_scripted_kwargs(turns=turns)).turns == turns

    def test_an_empty_script_is_rejected(self):
        with pytest.raises(ValidationError, match="at least 1"):
            SimulationConfig(**_scripted_kwargs(turns=[]))

    def test_a_blank_turn_is_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            SimulationConfig(**_scripted_kwargs(turns=["Fine.", "   "]))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("persona", "A business analyst."),
            ("goal", "Build an invoice pipeline."),
            ("constraints", ["Do not paste code."]),
            ("model", "anthropic.claude-haiku-4-5"),
            ("stop_token", "<<<STOP>>>"),
            ("stop_on_criteria_pass", True),
            ("check_criteria", "every_turn"),
        ],
    )
    def test_a_simulator_or_criteria_knob_beside_turns_is_rejected(self, field: str, value: Any):
        with pytest.raises(ValidationError, match=field):
            SimulationConfig(**_scripted_kwargs(**{field: value}))

    def test_a_recorded_config_validates_again(self):
        """``evaluate`` rebuilds the task from a full ``model_dump``, defaults included."""
        recorded = SimulationConfig(**_scripted_kwargs()).model_dump()
        assert SimulationConfig(**recorded).turns == ["First question.", "Second question."]

    @pytest.mark.parametrize("count", [1, 9, 100])
    @pytest.mark.parametrize("specified", [None, 1, 100])
    def test_script_length_sets_the_dialog_length(self, count, specified):
        kwargs = _scripted_kwargs(turns=[f"Question {i}." for i in range(count)])
        if specified is not None:
            kwargs["max_turns"] = specified
        cfg = SimulationConfig(**kwargs)
        assert cfg.max_turns == count
        assert SimulationConfig.model_validate(cfg.model_dump()).max_turns == count

    @pytest.mark.parametrize("invalid", [0, 101, 1.5, "invalid"])
    def test_raw_dialog_limit_still_validates_before_derivation(self, invalid):
        with pytest.raises(ValidationError, match="max_turns"):
            SimulationConfig(**_scripted_kwargs(max_turns=invalid))

    def test_script_keeps_the_existing_effective_upper_bound(self):
        with pytest.raises(ValidationError, match="turns"):
            SimulationConfig(**_scripted_kwargs(turns=["question"] * 101))

    def test_token_budget_and_trials_still_apply_to_a_script(self):
        cfg = SimulationConfig(**_scripted_kwargs(max_total_tokens=50_000, n_trials=3))
        assert (cfg.max_total_tokens, cfg.n_trials) == (50_000, 3)


class TestScriptedTurnsOnTaskDefinition:
    def _task(self, **overrides: Any) -> TaskDefinition:
        kwargs: dict[str, Any] = {
            "task_id": "scripted-task",
            "description": "A scripted task",
            "agent": parse_agent_config(type=AgentKind.CLAUDE_CODE),
            "sandbox": SandboxConfig(driver="tempdir"),
            "success_criteria": [FileExistsCriterion(path="out.txt", description="file exists")],
        }
        if "simulation" not in overrides:
            kwargs["simulation"] = SimulationConfig(**_scripted_kwargs())
        kwargs.update(overrides)
        return TaskDefinition(**kwargs)

    def test_no_initial_prompt_is_needed(self):
        task = self._task()
        assert task.initial_prompt is None

    def test_initial_prompt_beside_turns_is_rejected(self):
        simulation = SimulationConfig(**_scripted_kwargs())
        with pytest.raises(ValidationError, match="turns"):
            self._task(simulation=simulation, initial_prompt="Hello.")

    def test_initial_prompt_file_beside_turns_is_rejected(self):
        simulation = SimulationConfig(**_scripted_kwargs())
        with pytest.raises(ValidationError, match="turns"):
            self._task(simulation=simulation, initial_prompt_file="prompt.md")

    def test_a_disabled_script_leaves_the_prompt_contract_alone(self):
        simulation = SimulationConfig(**_scripted_kwargs(enabled=False))
        assert self._task(initial_prompt="Hello.", simulation=simulation).initial_prompt == "Hello."

    def test_the_working_directory_prefix_is_on_by_default(self):
        assert self._task().working_directory_prefix is True

    def test_the_working_directory_prefix_can_be_switched_off(self):
        assert self._task(working_directory_prefix=False).working_directory_prefix is False
