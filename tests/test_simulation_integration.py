"""End-to-end integration tests for the simulation dialog loop.

These tests run the real Orchestrator against a MockAgent and a UserSimulator
whose underlying Claude Code agent is replaced with a ``TextStubAgent`` (or
an exploding stub) via ``agent_override``, so they cover the full dialog
loop without touching any external LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

from coder_eval.agent import Agent, AgentState
from coder_eval.models import (
    AgentKind,
    FileExistsCriterion,
    SandboxConfig,
    SimulationConfig,
    TaskDefinition,
    TokenUsage,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator
from coder_eval.simulation.user_simulator import UserSimulator
from tests.fixtures.mock_agent import MockAgent
from tests.fixtures.text_stub_agent import TextStubAgent


def _build_task(
    sim_overrides: dict[str, Any] | None = None,
    *,
    initial_prompt: str | None = "Please create the file.",
) -> TaskDefinition:
    sim_kwargs: dict[str, Any] = {
        "enabled": True,
        "persona": "A business user",
        "goal": "make it work",
        "max_turns": 4,
        "stop_token": "<<<DONE>>>",
        "stop_on_criteria_pass": False,
        "check_criteria": "end_of_dialog",
    }
    if sim_overrides:
        sim_kwargs.update(sim_overrides)
    return TaskDefinition(
        task_id="sim-integration",
        description="Dialog-loop integration test",
        initial_prompt=initial_prompt,
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="test.txt", description="file must exist")],
        simulation=SimulationConfig(**sim_kwargs),
    )


def _install_fake_agent(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    async def _create(self):
        return MockAgent(self.task, scenario=scenario)

    monkeypatch.setattr(Orchestrator, "_create_agent", _create)


def _install_fake_simulator(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[str],
    *,
    tokens: tuple[int, int] = (5, 7),
) -> TextStubAgent:
    """Replace the simulator's internal Claude Code agent with a text stub.

    Returns the stub so tests can introspect the prompts it received
    (via ``stub.calls``).
    """
    stub = _TokenizedTextStub(responses, tokens=tokens)

    def factory(*args: Any, **kwargs: Any) -> UserSimulator:
        kwargs["agent_override"] = stub
        return UserSimulator(*args, **kwargs)

    monkeypatch.setattr("coder_eval.orchestrator.UserSimulator", factory)
    return stub


class _TokenizedTextStub(TextStubAgent):
    """TextStubAgent variant that attaches deterministic token counts to each TurnRecord."""

    def __init__(self, responses: list[str], *, tokens: tuple[int, int] = (5, 7)) -> None:
        super().__init__(responses)
        self._tokens = tokens

    async def communicate(self, user_input: str, **kwargs: object) -> TurnRecord:
        turn = await super().communicate(user_input, **kwargs)
        in_tok, out_tok = self._tokens
        return TurnRecord(
            iteration=turn.iteration,
            user_input=turn.user_input,
            agent_output=turn.agent_output,
            token_usage=TokenUsage(uncached_input_tokens=in_tok, output_tokens=out_tok),
        )


class _ExplodingAgent(Agent):
    """Agent fake whose communicate() always raises — exercises error-path handling."""

    def __init__(self, message: str = "simulator exploded") -> None:
        self._message = message
        self._state = AgentState.WORKING

    async def start(
        self,
        working_directory: str,
        *,
        env_path_prepend: list[str] | None = None,
        plugin_tools_dir: str | None = None,
    ) -> None:
        pass

    async def stop(self) -> None:
        self._state = AgentState.FINISHED

    def get_state(self) -> AgentState:
        return self._state

    async def communicate(self, user_input: str, **kwargs: object) -> TurnRecord:
        raise RuntimeError(self._message)


def _install_exploding_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(*args: Any, **kwargs: Any) -> UserSimulator:
        kwargs["agent_override"] = _ExplodingAgent()
        return UserSimulator(*args, **kwargs)

    monkeypatch.setattr("coder_eval.orchestrator.UserSimulator", factory)


@pytest.mark.asyncio
async def test_simulation_success_dialog_ends_on_stop_token(tmp_path, monkeypatch):
    """Happy path: MockAgent creates the file on turn 1, simulator emits stop on turn 2."""
    _install_fake_agent(monkeypatch, scenario="success")
    stub = _install_fake_simulator(monkeypatch, responses=["Looks good. <<<DONE>>>"])

    task = _build_task()
    run_dir = tmp_path / "run" / "sim"
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="default")

    result = await orch.run()

    assert result.final_status == "SUCCESS", result.final_status
    assert result.simulation is not None
    sim = result.simulation
    assert sim.stop_reason == "stop_token"
    assert sim.total_turns == 1
    # Simulator was invoked exactly once, after the first agent reply.
    assert len(stub.calls) == 1
    # The only message sent to the simulator is the agent's reply — session
    # resume carries the rest of the history.
    assert stub.calls[0] == "I've successfully completed all required file operations based on the task criteria."
    # Token accounting: one simulator turn with tokens (5, 7).
    assert sim.simulator_input_tokens == 5
    assert sim.simulator_output_tokens == 7


@pytest.mark.asyncio
async def test_simulation_hits_max_turns(tmp_path, monkeypatch):
    """When the simulator never stops, dialog terminates at max_turns."""
    _install_fake_agent(monkeypatch, scenario="failure")
    _install_fake_simulator(monkeypatch, responses=["keep trying"] * 10)

    task = _build_task({"max_turns": 3})
    orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="default")

    result = await orch.run()

    assert result.simulation is not None
    assert result.simulation.stop_reason == "max_turns"
    assert result.simulation.total_turns == 3
    assert result.final_status.value == "FAILURE"


@pytest.mark.asyncio
async def test_simulation_early_stop_on_criteria_pass(tmp_path, monkeypatch):
    """every_turn + stop_on_criteria_pass=True ends as soon as criteria pass."""
    _install_fake_agent(monkeypatch, scenario="success")
    _install_fake_simulator(monkeypatch, responses=["keep going"] * 10)

    task = _build_task(
        {
            "max_turns": 5,
            "check_criteria": "every_turn",
            "stop_on_criteria_pass": True,
        }
    )
    orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="default")

    result = await orch.run()

    assert result.simulation is not None
    assert result.simulation.stop_reason == "criteria_passed"
    assert result.simulation.total_turns == 1
    assert result.final_status.value == "SUCCESS"


@pytest.mark.asyncio
async def test_simulation_replicate_index_defaults_to_zero(tmp_path, monkeypatch):
    """Without n_trials expansion upstream, replicate_index defaults to 0."""
    _install_fake_agent(monkeypatch, scenario="success")
    _install_fake_simulator(monkeypatch, responses=["<<<DONE>>>"])

    task = _build_task()
    orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="default")

    result = await orch.run()
    assert result.simulation is not None
    assert result.simulation.replicate_index == 0
    assert result.simulation.n_trials == 1


@pytest.mark.asyncio
async def test_simulation_generates_opener_when_initial_prompt_absent(tmp_path, monkeypatch):
    """Pure-simulation mode: simulator produces turn-1's user message itself."""
    _install_fake_agent(monkeypatch, scenario="success")
    stub = _install_fake_simulator(
        monkeypatch,
        responses=["I want to build a little thing.", "Great, thanks. <<<DONE>>>"],
    )

    task = _build_task(initial_prompt=None)
    orch = Orchestrator(task=task, run_dir=tmp_path / "run" / "no-opener", variant_id="default")

    result = await orch.run()

    assert result.simulation is not None
    sim = result.simulation
    assert sim.stop_reason == "stop_token"
    # Exactly one agent turn happened (opener → agent → stop).
    assert sim.total_turns == 1
    # Two simulator calls: the opener (primer nudge) and the reaction to the agent's reply.
    assert len(stub.calls) == 2
    assert "Begin the conversation" in stub.calls[0]
    assert stub.calls[1] == "I've successfully completed all required file operations based on the task criteria."
    # The agent's first user_input should be the simulator-generated opener.
    assert result.iterations[0].user_input is not None
    assert "I want to build a little thing." in result.iterations[0].user_input
    # Token accounting aggregates both simulator calls.
    assert sim.simulator_input_tokens == 10  # 5 + 5
    assert sim.simulator_output_tokens == 14  # 7 + 7


@pytest.mark.asyncio
async def test_simulation_opener_failure_aborts_dialog(tmp_path, monkeypatch):
    """If the simulator fails on the very first call, the dialog aborts with stop_reason='error'."""
    _install_fake_agent(monkeypatch, scenario="success")
    _install_exploding_simulator(monkeypatch)

    task = _build_task(initial_prompt=None)
    orch = Orchestrator(task=task, run_dir=tmp_path / "run" / "opener-fail", variant_id="default")

    result = await orch.run()
    assert result.simulation is not None
    assert result.simulation.stop_reason == "error"
    assert result.simulation.simulator_failures == 1
    assert result.simulation.total_turns == 0


@pytest.mark.asyncio
async def test_simulation_opener_with_stop_token_short_circuits(tmp_path, monkeypatch):
    """Opener containing the stop token ends the dialog before any agent turn runs."""
    _install_fake_agent(monkeypatch, scenario="success")
    _install_fake_simulator(monkeypatch, responses=["Actually, nevermind — not needed. <<<DONE>>>"])

    task = _build_task(initial_prompt=None)
    orch = Orchestrator(task=task, run_dir=tmp_path / "run" / "opener-stop", variant_id="default")

    result = await orch.run()
    assert result.simulation is not None
    assert result.simulation.stop_reason == "stop_token"
    # Agent never ran — the simulator already signalled done in the opener.
    assert result.simulation.total_turns == 0
    assert len(result.iterations) == 0


@pytest.mark.asyncio
async def test_simulation_records_simulator_failure(tmp_path, monkeypatch):
    """If the simulator raises mid-dialog, it terminates with stop_reason='error'."""
    _install_fake_agent(monkeypatch, scenario="failure")
    _install_exploding_simulator(monkeypatch)

    task = _build_task()
    orch = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="default")

    result = await orch.run()
    assert result.simulation is not None
    assert result.simulation.stop_reason == "error"
    assert result.simulation.simulator_failures == 1


@pytest.mark.asyncio
async def test_simulation_pending_user_turn_prepended_to_each_turn(tmp_path, monkeypatch):
    """Each agent turn's ``messages[0]`` is the UserMessage that drove it — the pinned
    opener on turn 1, then each simulator utterance — and the end-of-dialog criteria
    check populates ``success_criteria_results``. This pins the seam most affected by the
    ``_solicit_user_message`` / ``_user_message_from_sim`` / ``pending_user_turn`` extraction.
    """
    _install_fake_agent(monkeypatch, scenario="failure")  # never satisfies criteria → dialog continues
    _install_fake_simulator(monkeypatch, responses=["try again please", "all good now <<<DONE>>>"])

    task = _build_task({"max_turns": 4})  # default initial_prompt is the pinned opener
    orch = Orchestrator(task=task, run_dir=tmp_path / "run" / "prepend", variant_id="default")

    result = await orch.run()

    assert result.simulation is not None
    assert result.simulation.stop_reason == "stop_token"
    assert result.simulation.total_turns == 2
    # Two real agent turns; each carries the driving user utterance as messages[0].
    assert result.iterations[0].messages[0].text == "Please create the file."
    assert result.iterations[1].messages[0].text == "try again please"
    # Token accounting aggregates the two simulator calls (5, 7) each.
    assert result.simulation.simulator_input_tokens == 10
    assert result.simulation.simulator_output_tokens == 14
    # End-of-dialog criteria check ran (success_criteria_results populated).
    assert result.success_criteria_results
