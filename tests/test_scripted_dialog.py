"""End-to-end tests for scripted dialogs: authored user turns, one agent session, no simulator.

These run the real ``Orchestrator`` (and, for ``execute`` / ``evaluate``, the real CLI)
against a ``MockAgent`` that records the prompts it receives, so they cover the whole
dialog loop without any LLM, ``claude`` CLI or credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app, run_helpers
from coder_eval.errors import TurnTimeoutError
from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    RunLimits,
    SandboxConfig,
    SimulationConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
    parse_agent_config,
    simulator_cost_usd,
)
from coder_eval.orchestrator import Orchestrator
from tests.fixtures.mock_agent import MockAgent


runner = CliRunner()

TURNS = [
    "What does the knowledge base say about onboarding?",
    "  And about\nreimbursements?  ",
    "Thanks — one more: «ünïcode» ✓",
]


class _SessionAgent(MockAgent):
    """A ``MockAgent`` that remembers every prompt, and can time out or run out of turns on cue."""

    def __init__(
        self,
        task: TaskDefinition,
        *,
        time_out_on_call: int | None = None,
        exhaust_on_call: int | None = None,
        tokens_per_call: int = 0,
    ) -> None:
        super().__init__(task, scenario="success")
        self.prompts: list[str] = []
        self._time_out_on_call = time_out_on_call
        self._exhaust_on_call = exhaust_on_call
        self._tokens_per_call = tokens_per_call

    async def communicate(self, user_input: str, **kwargs: Any) -> TurnRecord:
        self.prompts.append(user_input)
        call = len(self.prompts)
        if call == self._time_out_on_call:
            raise TurnTimeoutError(30.0, task_id=self.task.task_id, iteration=call)
        turn = await super().communicate(user_input, **kwargs)
        if self._tokens_per_call:
            turn.token_usage = TokenUsage(
                uncached_input_tokens=self._tokens_per_call, output_tokens=self._tokens_per_call
            )
        turn.max_turns_exhausted = call == self._exhaust_on_call
        return turn


def _install_agent(monkeypatch: pytest.MonkeyPatch, **agent_kwargs: Any) -> list[_SessionAgent]:
    """Make every orchestrator build a ``_SessionAgent``; the returned list collects them."""
    created: list[_SessionAgent] = []

    async def _create(self: Orchestrator) -> _SessionAgent:
        created.append(_SessionAgent(self.task, **agent_kwargs))
        return created[-1]

    monkeypatch.setattr(Orchestrator, "_create_agent", _create)
    return created


def _task(turns: list[str] = TURNS, *, verbatim: bool = True, **overrides: Any) -> TaskDefinition:
    """A scripted task; ``verbatim=False`` leaves ``working_directory_prefix`` at its default."""
    kwargs: dict[str, Any] = {
        "task_id": "scripted",
        "description": "A scripted dialog",
        "agent": parse_agent_config(type=AgentKind.CLAUDE_CODE),
        "sandbox": SandboxConfig(driver="tempdir"),
        "success_criteria": [FileExistsCriterion(path="test.txt", description="file must exist")],
    }
    if verbatim:
        kwargs["working_directory_prefix"] = False
    if "simulation" not in overrides:
        kwargs["simulation"] = SimulationConfig(enabled=True, turns=turns, max_turns=len(turns))
    kwargs.update(overrides)
    return TaskDefinition(**kwargs)


async def _run(tmp_path: Path, task: TaskDefinition, *, grade: bool = True) -> EvaluationResult:
    return await Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="default", grade=grade).run()


# --------------------------------------------------------------------------
# What the agent receives
# --------------------------------------------------------------------------


async def test_turns_reach_the_agent_verbatim_and_in_order_within_one_session(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)

    result = await _run(tmp_path, _task())

    assert len(created) == 1, "one agent instance is one session"
    assert created[0].prompts == TURNS
    assert [turn.user_input for turn in result.iterations] == TURNS
    assert [turn.messages[0].text for turn in result.iterations] == TURNS


async def test_each_turn_keeps_the_working_directory_prefix_unless_the_task_opts_out(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)

    await _run(tmp_path, _task(verbatim=False))

    prefix = f"Your working directory is: {created[0].working_directory.resolve()}\n\n"
    assert created[0].prompts == [prefix + turn for turn in TURNS]


async def test_a_single_shot_prompt_can_drop_the_working_directory_prefix(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)
    single_shot = _task(simulation=None, initial_prompt="Say exactly this.")

    await _run(tmp_path, single_shot)

    assert created[0].prompts == ["Say exactly this."]


async def test_a_single_shot_prompt_keeps_the_working_directory_prefix_by_default(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)
    single_shot = _task(simulation=None, initial_prompt="Say exactly this.", verbatim=False)

    await _run(tmp_path, single_shot)

    prefix = f"Your working directory is: {created[0].working_directory.resolve()}\n\n"
    assert created[0].prompts == [prefix + "Say exactly this."]


# --------------------------------------------------------------------------
# How the dialog ends, and what it records
# --------------------------------------------------------------------------


async def test_a_finished_script_reports_script_exhausted_and_costs_the_user_side_nothing(tmp_path, monkeypatch):
    _install_agent(monkeypatch)

    result = await _run(tmp_path, _task())

    sim = result.simulation
    assert sim is not None
    assert sim.stop_reason == "script_exhausted"
    assert sim.total_turns == len(TURNS)
    assert (sim.simulator_input_tokens, sim.simulator_output_tokens, sim.simulator_failures) == (0, 0, 0)
    assert sim.simulator_model is None
    assert simulator_cost_usd(result) is None
    assert result.iteration_count == len(TURNS)
    assert len(result.iterations) == len(TURNS), "no trailing record for a user turn nobody answered"


async def test_the_script_runs_to_its_end_even_when_the_criteria_already_pass(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)
    checks: list[None] = []
    original = Orchestrator._run_dialog_criteria_check

    async def _counting(self: Orchestrator, accum: Any) -> Any:
        checks.append(None)
        return await original(self, accum)

    monkeypatch.setattr(Orchestrator, "_run_dialog_criteria_check", _counting)

    result = await _run(tmp_path, _task())

    # The mock agent writes test.txt on its first reply, so the criteria pass from turn 1.
    assert created[0].prompts == TURNS
    assert result.final_status == FinalStatus.SUCCESS
    assert len(checks) == 1, "criteria are read once, after the last turn, never to steer the dialog"


async def test_the_conversation_log_shows_the_script_as_authored_not_simulated(tmp_path, monkeypatch):
    _install_agent(monkeypatch)

    await _run(tmp_path, _task())

    log = (tmp_path / "run" / "conversation.log").read_text(encoding="utf-8")
    user_headers = re.findall(r"^=== USER \(turn (\d+)\)(?: — (.*))? ===$", log, flags=re.MULTILINE)
    assert user_headers == [("1", "pinned opener"), ("2", ""), ("3", "")]


async def test_no_simulator_is_built_for_a_script(tmp_path, monkeypatch):
    _install_agent(monkeypatch)

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a scripted dialog must not build an LLM simulator")

    monkeypatch.setattr("coder_eval.orchestrator.UserSimulator", _forbidden)

    result = await _run(tmp_path, _task())

    assert result.simulation is not None
    assert result.simulation.stop_reason == "script_exhausted"


# --------------------------------------------------------------------------
# Without grading (`execute`)
# --------------------------------------------------------------------------


async def test_a_script_runs_in_full_ungraded_and_finalizes_not_graded(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch)

    result = await _run(tmp_path, _task(), grade=False)

    assert created[0].prompts == TURNS
    assert result.final_status == FinalStatus.NOT_GRADED
    assert result.weighted_score is None
    assert result.success_criteria_results == []
    assert result.simulation is not None
    assert result.simulation.stop_reason == "script_exhausted"


async def test_a_budget_breach_ends_an_ungraded_script_with_its_own_status(tmp_path, monkeypatch):
    """The partial-credit check a breach normally runs must not fire (and raise) when nothing is graded."""
    created = _install_agent(monkeypatch, tokens_per_call=100)
    task = _task(run_limits=RunLimits(max_total_tokens=150))

    result = await _run(tmp_path, task, grade=False)

    assert created[0].prompts == TURNS[:1]
    assert result.final_status == FinalStatus.TOKEN_BUDGET_EXCEEDED
    assert result.simulation is not None
    assert result.simulation.stop_reason == "run_limit_exceeded"


_SCRIPTED_YAML = """task_id: scripted-cli
description: A scripted dialog run through the CLI
agent:
  type: claude-code
working_directory_prefix: false
simulation:
  enabled: true
  turns:
    - First question.
    - Second question.
  max_turns: 2
success_criteria:
  - type: file_exists
    path: test.txt
    description: file must exist
"""


def _single_task_dir(run_dir: Path) -> Path:
    (task_json,) = run_dir.glob("**/task.json")
    return task_json.parent


def test_execute_accepts_a_script_and_evaluate_grades_it_afterwards(tmp_path, monkeypatch):
    monkeypatch.setattr(run_helpers.settings, "runs_dir", tmp_path / "default-runs")
    created = _install_agent(monkeypatch)
    task_file = tmp_path / "task.yaml"
    task_file.write_text(_SCRIPTED_YAML, encoding="utf-8")
    run_dir = tmp_path / "run"

    executed = runner.invoke(app, ["execute", str(task_file), "--run-dir", str(run_dir)])

    assert executed.exit_code == 0, executed.output
    assert created[0].prompts == ["First question.", "Second question."]
    task_dir = _single_task_dir(run_dir)
    row = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert row["final_status"] == "NOT_GRADED"
    assert row["simulation"]["stop_reason"] == "script_exhausted"

    graded = runner.invoke(app, ["evaluate", str(task_dir)])

    assert graded.exit_code == 0, graded.output
    row = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    assert row["final_status"] == "SUCCESS"
    assert row["simulation"]["stop_reason"] == "script_exhausted"
    assert len(row["iterations"]) == 2


# --------------------------------------------------------------------------
# A turn that fails
# --------------------------------------------------------------------------


async def test_a_failed_turn_ends_the_script_and_later_turns_are_never_sent(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch, time_out_on_call=2)

    result = await _run(tmp_path, _task())

    assert created[0].prompts == TURNS[:2], "turn 2 was sent and failed; turn 3 never was"
    assert result.final_status == FinalStatus.ERROR
    assert "timed out" in (result.error_message or "")
    assert result.simulation is not None
    assert result.simulation.stop_reason == "error"
    assert TURNS[1] in [turn.user_input for turn in result.iterations], "the turn that failed stays on record"


async def test_an_agent_that_runs_out_of_its_own_turns_ends_the_script(tmp_path, monkeypatch):
    created = _install_agent(monkeypatch, exhaust_on_call=1)

    result = await _run(tmp_path, _task())

    assert created[0].prompts == TURNS[:1]
    assert result.max_turns_exhausted is True
    assert result.simulation is not None
    assert result.simulation.stop_reason == "agent_max_turns"
