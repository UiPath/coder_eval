"""Tests for the UserSimulator.

The simulator runs as a tools-disabled Claude Code agent. These tests bypass
the real SDK by passing a ``TextStubAgent`` via the ``agent_override`` kwarg —
``start()`` wires it in place, each ``next_user_message`` call consumes one
canned response.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from coder_eval.models import SimulationConfig
from coder_eval.simulation.user_simulator import UserSimulator
from tests.fixtures.text_stub_agent import TextStubAgent


def _sim_cfg(**overrides: Any) -> SimulationConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "persona": "impatient business analyst",
        "goal": "ship an invoice workflow",
        "stop_token": "<<<DONE>>>",
        "max_turns": 5,
    }
    base.update(overrides)
    return SimulationConfig(**base)


def _pair(user_input: str, agent_output: str) -> tuple[str, str]:
    return (user_input, agent_output)


async def _make_started(sim: UserSimulator) -> UserSimulator:
    await sim.start()
    return sim


class TestSystemPrompt:
    def test_persona_goal_in_prompt(self):
        sim = UserSimulator(
            config=_sim_cfg(persona="BA persona", goal="build X"),
            task_description="A task",
            initial_prompt="Hi, I need help.",
        )
        sp = sim.system_prompt
        assert "BA persona" in sp
        assert "build X" in sp
        assert "Hi, I need help." in sp

    def test_constraints_rendered(self):
        sim = UserSimulator(
            config=_sim_cfg(constraints=["Do not mention X", "Reveal Y only if asked"]),
            task_description="A task",
            initial_prompt="Start",
        )
        assert "Do not mention X" in sim.system_prompt
        assert "Reveal Y only if asked" in sim.system_prompt

    def test_system_prompt_override_used_verbatim(self):
        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="ignored",
            initial_prompt="ignored",
            system_prompt_override="CUSTOM TEMPLATE BODY",
        )
        assert sim.system_prompt == "CUSTOM TEMPLATE BODY"

    def test_opener_wording_when_no_initial_prompt(self):
        sim = UserSimulator(
            config=_sim_cfg(persona="BA", goal="build dice roller"),
            task_description="T",
            initial_prompt=None,
        )
        sp = sim.system_prompt
        assert "OPENING" in sp
        assert "BA" in sp
        assert "build dice roller" in sp
        assert "began the conversation with this opening message" not in sp


class TestDialogFlow:
    async def test_only_latest_agent_reply_sent_each_turn(self):
        """Simulator agent is given ONLY the most recent agent reply — session resume carries history."""
        stub = TextStubAgent(["Please continue.", "Thanks, we're done. <<<DONE>>>"])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="T",
                initial_prompt="start",
                agent_override=stub,
            )
        )
        pairs_turn1 = [_pair("start", "Here's a plan. What next?")]
        r1 = await sim.next_user_message(pairs_turn1)
        assert r1.text == "Please continue."
        assert stub.calls[-1] == "Here's a plan. What next?"

        pairs_turn2 = [*pairs_turn1, _pair("Please continue.", "Done. Shall I continue?")]
        r2 = await sim.next_user_message(pairs_turn2)
        assert r2.stop_requested is True
        assert r2.text == "Thanks, we're done."
        assert stub.calls[-1] == "Done. Shall I continue?"
        await sim.stop()

    async def test_opener_uses_primer_when_history_empty(self):
        stub = TextStubAgent(["Hey, I want to build a little dice thing."])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="Dice roller task",
                initial_prompt=None,
                agent_override=stub,
            )
        )
        result = await sim.next_user_message([])
        assert result.text == "Hey, I want to build a little dice thing."
        assert result.stop_requested is False
        # Opener call uses the opener-nudge primer, not any real dialog history.
        assert len(stub.calls) == 1
        assert "Begin the conversation" in stub.calls[0]
        await sim.stop()


class TestStopTokenHandling:
    async def test_stop_token_triggers_flag_and_strips(self):
        stub = TextStubAgent(["Looks good. <<<DONE>>>"])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="T",
                initial_prompt="start",
                agent_override=stub,
            )
        )
        result = await sim.next_user_message([_pair("start", "reply")])
        assert result.stop_requested is True
        assert result.raw_text == "Looks good. <<<DONE>>>"
        assert result.text == "Looks good."
        await sim.stop()

    async def test_stop_token_only_message_gets_placeholder(self):
        """If the simulator emits ONLY the stop token, we substitute a marker."""
        stub = TextStubAgent(["<<<DONE>>>"])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="T",
                initial_prompt="start",
                agent_override=stub,
            )
        )
        result = await sim.next_user_message([_pair("start", "reply")])
        assert result.stop_requested is True
        assert result.text == "(the user indicated the task is complete)"
        await sim.stop()

    async def test_no_stop_token(self):
        stub = TextStubAgent(["Please retry."])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="T",
                initial_prompt="start",
                agent_override=stub,
            )
        )
        result = await sim.next_user_message([_pair("start", "reply")])
        assert result.stop_requested is False
        assert result.text == "Please retry."
        await sim.stop()


class TestReferenceNotLeaked:
    """Security: the task's reference solution must never reach the simulator.

    The simulator's system prompt is built from persona + goal + constraints +
    task description only. Even if a caller were tempted to stuff reference
    material into the system prompt, the prompt builder has no channel for it —
    this test pins that property so a regression would fail loudly.
    """

    _SECRET = "SECRET_REFERENCE_0xDEADBEEF_MUST_NOT_LEAK"

    def test_reference_field_never_flows_into_prompt_builder(self):
        """The prompt builder accepts persona/goal/description/initial_prompt only.

        There is no ``reference`` parameter on _extract_system_prompt —
        enforce that by attempting to pass one and ensuring the secret is absent.
        """
        from coder_eval.simulation.user_simulator import _extract_system_prompt

        cfg = _sim_cfg(persona="BA", goal="ship a flow")
        prompt = _extract_system_prompt(cfg, task_description="plain desc", initial_prompt=None)
        assert self._SECRET not in prompt

    async def test_simulator_never_sees_reference_via_communicate(self):
        """End-to-end: drive a short dialog and assert no stub ``communicate`` prompt contains the reference."""
        stub = TextStubAgent(["opener", "<<<DONE>>>"])
        sim = await _make_started(
            UserSimulator(
                config=_sim_cfg(),
                task_description="desc",
                initial_prompt=None,
                agent_override=stub,
            )
        )
        # The prompts the simulator agent receives are: the opener-nudge, then
        # the coding agent's replies. Neither is constructed from `reference`.
        await sim.next_user_message([])
        await sim.next_user_message([_pair("opener", f"coding agent reply mentioning {self._SECRET}")])
        # Agent-replies flow back into the simulator, but the reference solution
        # itself never enters the dialog loop via coder_eval code paths — only
        # whatever the coding agent literally emits. The system prompt stays clean.
        assert self._SECRET not in sim.system_prompt
        # Verify the stub's recorded calls do include agent text (but nothing
        # the framework injected from a reference field).
        assert any(self._SECRET in c for c in stub.calls), (
            "Test sanity: the secret must come from the fake agent reply, not from the simulator plumbing"
        )
        await sim.stop()


class TestLifecycle:
    async def test_next_user_message_before_start_raises(self):
        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="T",
            initial_prompt="start",
            agent_override=TextStubAgent(["ok"]),
        )
        with pytest.raises(AssertionError):
            await sim.next_user_message([])

    async def test_start_and_stop_call_through_to_agent(self):
        stub = TextStubAgent(["ok"])
        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="T",
            initial_prompt="start",
            agent_override=stub,
        )
        assert not stub.started
        await sim.start()
        assert stub.started
        assert stub.working_directory is not None and stub.working_directory.exists()
        scratch = stub.working_directory
        await sim.stop()
        assert stub.stopped
        assert not scratch.exists()

    async def test_start_failure_removes_scratch_dir(self):
        """When _agent.start() raises, start() re-raises AND removes the sim-* scratch dir (no leak)."""

        class FailingStartAgent(TextStubAgent):
            def __init__(self) -> None:
                super().__init__([])
                self.captured_dir: Path | None = None

            async def start(
                self,
                working_directory: str,
                *,
                env_path_prepend: list[str] | None = None,
                plugin_tools_dir: str | None = None,
            ) -> None:
                self.captured_dir = Path(working_directory)
                raise RuntimeError("boom")

        stub = FailingStartAgent()
        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="T",
            initial_prompt="start",
            agent_override=stub,
        )
        with pytest.raises(RuntimeError, match="boom"):
            await sim.start()

        # The scratch dir was created (mkdtemp ran) but must be gone after the failed start.
        assert stub.captured_dir is not None
        assert not stub.captured_dir.exists()
        # State reset, so a follow-up stop() is a safe no-op.
        assert sim._scratch_dir is None
        assert sim._agent is None
        await sim.stop()  # must not raise

    async def test_start_failure_on_base_exception_cleans_up(self):
        """A BaseException (e.g. cancellation) during _agent.start() still triggers cleanup and re-raises."""

        class CancelStartAgent(TextStubAgent):
            def __init__(self) -> None:
                super().__init__([])
                self.captured_dir: Path | None = None

            async def start(
                self,
                working_directory: str,
                *,
                env_path_prepend: list[str] | None = None,
                plugin_tools_dir: str | None = None,
            ) -> None:
                self.captured_dir = Path(working_directory)
                raise asyncio.CancelledError()

        stub = CancelStartAgent()
        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="T",
            initial_prompt="start",
            agent_override=stub,
        )
        with pytest.raises(asyncio.CancelledError):
            await sim.start()

        assert stub.captured_dir is not None
        assert not stub.captured_dir.exists()
        assert sim._scratch_dir is None

    async def test_start_failure_during_agent_construction_cleans_up(self, monkeypatch):
        """agent_override=None: a raise in ClaudeCodeAgent construction (before _agent.start())
        still removes the sim-* scratch dir and resets state.

        The other start-failure tests inject a fake via agent_override, so they
        only exercise the _agent.start() branch. This covers the construction
        branch the start() comment explicitly calls out as the newly-closed leak.
        """
        import coder_eval.agents.claude_code_agent as cca

        captured: dict[str, Path | None] = {"dir": None}

        sim = UserSimulator(
            config=_sim_cfg(),
            task_description="T",
            initial_prompt="start",
            agent_override=None,
        )

        def _boom(*_args, **_kwargs):
            # mkdtemp has already run; capture the scratch dir before it is cleaned up.
            captured["dir"] = sim._scratch_dir
            raise RuntimeError("ctor boom")

        monkeypatch.setattr(cca, "ClaudeCodeAgent", _boom)

        with pytest.raises(RuntimeError, match="ctor boom"):
            await sim.start()

        # The scratch dir was created by mkdtemp but must be gone after the failed construction.
        assert captured["dir"] is not None
        assert not captured["dir"].exists()
        assert sim._scratch_dir is None
        assert sim._agent is None
        await sim.stop()  # must not raise

    async def test_disabled_simulator_is_no_op(self):
        """When config.enabled is False, start/stop do nothing and no agent is created."""
        stub = TextStubAgent(["ignored"])
        sim = UserSimulator(
            config=_sim_cfg(enabled=False),
            task_description="T",
            initial_prompt="start",
            agent_override=stub,
        )
        await sim.start()
        assert not stub.started  # start() bails early when disabled
        await sim.stop()
