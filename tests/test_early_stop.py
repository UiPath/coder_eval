"""Tests for early-stop-on-criterion: config surface, live-verdict
contract, and resolution-time guardrails.

This suite is inert at runtime (nothing invokes the interrupt yet); these tests
cover the pieces that DO exist: the two new opt-in config fields, the
``live_verdict`` / ``live_stop_polarities`` observability contract on the two
observable criteria, and ``validate_early_stop``'s guardrails on both the
``plan`` and ``run`` surfaces.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.criteria import CriterionRegistry, init_criteria
from coder_eval.criteria.base import BaseCriterion
from coder_eval.criteria.command_executed import CommandExecutedChecker
from coder_eval.criteria.skill_triggered import SkillTriggeredChecker, _engaged_skill_names
from coder_eval.models import (
    AgentKind,
    CommandExecutedCriterion,
    CommandTelemetry,
    FileExistsCriterion,
    RunLimits,
    SandboxConfig,
    SimulationConfig,
    SkillTriggeredCriterion,
    TaskDefinition,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestration.early_stop import EarlyStopConfigError, validate_early_stop
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, TurnEndStatus


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_TS = datetime(2026, 1, 1, 0, 0, 0)


def _cmd(tool_name: str, parameters: dict[str, Any], *, result_status: str = "success") -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=f"tool-{tool_name}",
        timestamp=_TS,
        parameters=parameters,
        result_status=result_status,
    )


def _turn(*commands: CommandTelemetry) -> TurnRecord:
    return TurnRecord(iteration=1, user_input="", agent_output="", commands=list(commands))


def _task(
    *,
    criteria: list[Any],
    stop_early: bool = False,
    agent_type: AgentKind = AgentKind.CLAUDE_CODE,
    simulation: SimulationConfig | None = None,
) -> TaskDefinition:
    """Build a minimal resolved-style TaskDefinition for guardrail tests."""
    return TaskDefinition(
        task_id="early-stop-test",
        description="early-stop test task",
        initial_prompt="do the thing",
        agent=parse_agent_config(type=agent_type),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=criteria,
        run_limits=RunLimits(stop_early=stop_early, max_turns=20),
        simulation=simulation,
    )


def _skill_crit(skill_name: str, expected_skill: str, *, stop_when: str | None = None) -> SkillTriggeredCriterion:
    return SkillTriggeredCriterion(
        type="skill_triggered",
        description=f"{skill_name} activation",
        skill_name=skill_name,
        expected_skill=expected_skill,
        stop_when=stop_when,  # type: ignore[arg-type]
    )


def _cmd_crit(
    *, min_count: int = 1, max_count: int | None = None, pattern: str | None = "curl", stop_when: str | None = None
) -> CommandExecutedCriterion:
    return CommandExecutedCriterion(
        type="command_executed",
        description="command check",
        tool_name="Bash",
        command_pattern=pattern,
        min_count=min_count,
        max_count=max_count,
        stop_when=stop_when,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Config surface
# --------------------------------------------------------------------------- #


class TestConfigSurface:
    def test_stop_early_defaults_false(self) -> None:
        assert RunLimits().stop_early is False

    def test_stop_early_settable(self) -> None:
        assert RunLimits(stop_early=True).stop_early is True

    def test_stop_when_defaults_none(self) -> None:
        assert _skill_crit("s", "s").stop_when is None

    @pytest.mark.parametrize("value", ["pass", "fail", "decided"])
    def test_stop_when_accepts_valid_polarities(self, value: str) -> None:
        assert _skill_crit("s", "s", stop_when=value).stop_when == value

    def test_stop_when_rejects_invalid_polarity(self) -> None:
        with pytest.raises(ValueError):
            _skill_crit("s", "s", stop_when="maybe")


# --------------------------------------------------------------------------- #
# skill_triggered live verdict
# --------------------------------------------------------------------------- #


class TestEngagedSkillNames:
    def test_claude_skill_tool_namespaced(self) -> None:
        assert _engaged_skill_names(_cmd("Skill", {"skill": "plugin:date-teller"})) == {"date-teller"}

    def test_bare_skill_param(self) -> None:
        assert _engaged_skill_names(_cmd("Skill", {"skill": "date-teller"})) == {"date-teller"}

    def test_file_read_path_segment(self) -> None:
        got = _engaged_skill_names(_cmd("Read", {"file_path": "/repo/skills/date-teller/SKILL.md"}))
        assert got == {"date-teller"}

    def test_bash_command_path_segment(self) -> None:
        assert _engaged_skill_names(_cmd("Bash", {"command": "cat skills/foo/refs.md"})) == {"foo"}

    def test_no_engagement(self) -> None:
        assert _engaged_skill_names(_cmd("Bash", {"command": "echo hi"})) == set()


class TestSkillTriggeredLiveVerdict:
    checker = SkillTriggeredChecker()

    def test_undecided_before_any_engagement(self) -> None:
        crit = _skill_crit("date-teller", "date-teller")
        assert self.checker.live_verdict(crit, [_turn(_cmd("Bash", {"command": "ls"}))]) == "undecided"

    def test_pass_when_expected_skill_engaged(self) -> None:
        crit = _skill_crit("date-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "plugin:date-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "pass"

    def test_fail_when_wrong_skill_engaged(self) -> None:
        # Positive row expecting date-teller, but a different skill loads first.
        crit = _skill_crit("date-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "other-skill"}))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_negative_row_target_engaged_is_fail(self) -> None:
        # expected_skill == "" (negative): engaging the target skill fails.
        crit = _skill_crit("date-teller", "")
        rec = [_turn(_cmd("Skill", {"skill": "date-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_negative_row_other_engaged_is_pass(self) -> None:
        crit = _skill_crit("date-teller", "")
        rec = [_turn(_cmd("Skill", {"skill": "unrelated"}))]
        assert self.checker.live_verdict(crit, rec) == "pass"

    def test_first_engagement_decides(self) -> None:
        # The wrong skill engages first, the expected one later — first wins.
        crit = _skill_crit("date-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "wrong"}), _cmd("Skill", {"skill": "date-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_polarities_declared(self) -> None:
        assert SkillTriggeredChecker.live_stop_polarities == frozenset({"pass", "fail"})


# --------------------------------------------------------------------------- #
# command_executed live verdict
# --------------------------------------------------------------------------- #


class TestCommandExecutedLiveVerdict:
    checker = CommandExecutedChecker()

    def _bash(self, cmd: str) -> CommandTelemetry:
        return _cmd("Bash", {"command": cmd})

    def test_undecided_below_min(self) -> None:
        crit = _cmd_crit(min_count=2, pattern="curl")
        assert self.checker.live_verdict(crit, [_turn(self._bash("curl x"))]) == "undecided"

    def test_pass_at_min_when_no_upper_bound(self) -> None:
        crit = _cmd_crit(min_count=1, pattern="curl")
        assert self.checker.live_verdict(crit, [_turn(self._bash("curl x"))]) == "pass"

    def test_must_not_run_first_match_is_fail(self) -> None:
        crit = _cmd_crit(min_count=0, max_count=0, pattern="rm ")
        assert self.checker.live_verdict(crit, [_turn(self._bash("rm -rf /"))]) == "fail"

    def test_must_not_run_no_match_is_undecided(self) -> None:
        # Absence of the forbidden event isn't decidable mid-run.
        crit = _cmd_crit(min_count=0, max_count=0, pattern="rm ")
        assert self.checker.live_verdict(crit, [_turn(self._bash("ls"))]) == "undecided"

    def test_range_never_live_passes(self) -> None:
        crit = _cmd_crit(min_count=2, max_count=3, pattern="x")
        rec = [_turn(self._bash("x"), self._bash("x"))]  # within range
        assert self.checker.live_verdict(crit, rec) == "undecided"

    def test_range_over_max_is_fail(self) -> None:
        crit = _cmd_crit(min_count=1, max_count=2, pattern="x")
        rec = [_turn(self._bash("x"), self._bash("x"), self._bash("x"))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_invalid_regex_is_undecided(self) -> None:
        crit = _cmd_crit(min_count=1, pattern="[")
        assert self.checker.live_verdict(crit, [_turn(self._bash("anything"))]) == "undecided"

    def test_zero_min_no_max_is_undecided(self) -> None:
        # min_count=0 + no upper bound has nothing to wait for and no fail edge.
        crit = _cmd_crit(min_count=0, max_count=None, pattern="curl")
        assert self.checker.live_verdict(crit, [_turn(self._bash("ls"))]) == "undecided"

    def test_matching_shared_with_check_impl(self) -> None:
        # The live count and the authoritative check agree on matches.
        crit = _cmd_crit(min_count=1, pattern="curl")
        rec = [_turn(self._bash("curl a"), self._bash("wget b"), self._bash("curl c"))]
        assert self.checker.live_verdict(crit, rec) == "pass"
        # _check_impl scores 1.0 on the same trajectory (>= min_count).
        result = self.checker.check(crit, sandbox=None, turn_records=rec)  # type: ignore[arg-type]
        assert result.score == 1.0

    def test_polarities_declared(self) -> None:
        assert CommandExecutedChecker.live_stop_polarities == frozenset({"pass", "fail"})


# --------------------------------------------------------------------------- #
# Base default: unobservable criteria
# --------------------------------------------------------------------------- #


class TestBaseLiveVerdictDefault:
    def test_base_polarities_empty(self) -> None:
        assert BaseCriterion.live_stop_polarities == frozenset()

    def test_unobservable_checker_is_undecided(self) -> None:
        init_criteria(validate=False)
        checker = CriterionRegistry.get_checker("file_exists")()
        assert checker.live_stop_polarities == frozenset()
        crit = FileExistsCriterion(type="file_exists", path="x.txt", description="x")
        assert checker.live_verdict(crit, [_turn()]) == "undecided"


# --------------------------------------------------------------------------- #
# Resolution-time guardrails
# --------------------------------------------------------------------------- #


class TestValidateEarlyStop:
    def test_unarmed_is_noop_even_with_bad_shape(self) -> None:
        # stop_early=False → validator never inspects anything.
        task = _task(criteria=[_skill_crit("s", "s")], stop_early=False, agent_type=AgentKind.CODEX)
        validate_early_stop(task)  # no raise

    def test_unarmed_with_stop_when_is_inert(self) -> None:
        # A criterion may declare stop_when; without stop_early it stays inert.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=False)
        validate_early_stop(task)  # no raise

    def test_armed_happy_path_accepts(self) -> None:
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_armed_command_executed_accepts(self) -> None:
        task = _task(criteria=[_cmd_crit(stop_when="fail")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_guardrail5_simulation_rejected(self) -> None:
        sim = SimulationConfig(enabled=True, persona="user", goal="get it done")
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=True, simulation=sim)
        with pytest.raises(EarlyStopConfigError, match="simulation"):
            validate_early_stop(task)

    def test_guardrail1_non_claude_agent_rejected(self) -> None:
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=True, agent_type=AgentKind.CODEX)
        with pytest.raises(EarlyStopConfigError, match="cooperative stopping"):
            validate_early_stop(task)

    def test_guardrail2_no_stop_criterion_rejected(self) -> None:
        task = _task(criteria=[_skill_crit("s", "s")], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="at least one stop criterion"):
            validate_early_stop(task)

    def test_guardrail3_unobservable_criterion_rejected(self) -> None:
        crit = FileExistsCriterion(type="file_exists", path="x.txt", description="x", stop_when="pass")
        task = _task(criteria=[crit], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="observable"):
            validate_early_stop(task)

    def test_guardrail4_unsupported_polarity_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force command_executed to be pass-only, then arm it with stop_when="fail".
        init_criteria(validate=False)
        monkeypatch.setattr(CommandExecutedChecker, "live_stop_polarities", frozenset({"pass"}))
        task = _task(criteria=[_cmd_crit(stop_when="fail")], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="polarity"):
            validate_early_stop(task)

    def test_raise_order_simulation_before_agent(self) -> None:
        # Both simulation AND a non-Claude agent are invalid; simulation reports first.
        sim = SimulationConfig(enabled=True, persona="user", goal="g")
        task = _task(
            criteria=[_skill_crit("s", "s", stop_when="decided")],
            stop_early=True,
            agent_type=AgentKind.CODEX,
            simulation=sim,
        )
        with pytest.raises(EarlyStopConfigError, match="simulation"):
            validate_early_stop(task)

    def test_stacked_activation_criteria_accept(self) -> None:
        # Multiple armed skill_triggered criteria (the activation pattern).
        crits = [
            _skill_crit("skill-a", "skill-a", stop_when="decided"),
            _skill_crit("skill-b", "skill-a", stop_when="decided"),
        ]
        task = _task(criteria=crits, stop_early=True)
        validate_early_stop(task)  # no raise


# --------------------------------------------------------------------------- #
# Cooperative should_stop seam on ClaudeCodeAgent — still UNWIRED: the
# orchestrator does not pass should_stop yet, so these drive the agent directly.
# --------------------------------------------------------------------------- #


class _DummyMsg:
    """Minimal SDK-message stand-in.

    ``dispatch`` records it and matches no message predicate (so it is ignored),
    and it exposes no ``.error`` attribute so ``_update_state_from_messages``
    leaves the agent in WORKING — unlike a MagicMock, whose ``.error`` would be a
    truthy mock and wrongly flip the state to ERROR.
    """

    def __init__(self, index: int) -> None:
        self.index = index


class _EventSink:
    """StreamCallback that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


async def _run_claude_communicate(
    *, stop_after: int | None = None, never: bool = False, n_messages: int = 3
) -> tuple[ClaudeCodeAgent, TurnRecord, _EventSink, int]:
    """Drive ``ClaudeCodeAgent.communicate`` over a mocked ``query`` yielding
    ``n_messages`` dummy messages.

    ``stop_after``: build a should_stop that returns True once that many messages
    have been pulled (checked after each dispatch). ``never``: pass an explicit
    always-False should_stop. Neither: pass ``should_stop=None``. Returns
    ``(agent, record, sink, pulled_count)``.
    """
    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)
    pulled = {"n": 0}

    should_stop: Callable[[], bool] | None
    if stop_after is not None:

        def should_stop() -> bool:
            return pulled["n"] >= stop_after

    elif never:

        def should_stop() -> bool:
            return False

    else:
        should_stop = None

    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def mock_query(prompt: Any, options: Any, transport: Any = None) -> Any:
            for i in range(n_messages):
                pulled["n"] += 1
                yield _DummyMsg(i)

        sink = _EventSink()
        with patch("coder_eval.agents.claude_code_agent.query", mock_query):
            record = await agent.communicate("prompt", stream_callback=sink, should_stop=should_stop)
    return agent, record, sink, pulled["n"]


def _agent_end_events(sink: _EventSink) -> list[AgentEndEvent]:
    return [e for e in sink.events if isinstance(e, AgentEndEvent)]


class TestCooperativeStopSeam:
    def test_stopped_early_member_on_both_enums(self) -> None:
        assert AgentEndStatus.STOPPED_EARLY.value == "stopped_early"
        assert TurnEndStatus.STOPPED_EARLY.value == "stopped_early"

    def test_turnendstatus_conversion_from_agentendstatus(self) -> None:
        # finalize() (and antigravity_agent) convert via TurnEndStatus(status.value);
        # the new member must round-trip so an early-stopped open turn isn't mislabeled.
        assert TurnEndStatus(AgentEndStatus.STOPPED_EARLY.value) == TurnEndStatus.STOPPED_EARLY

    async def test_stop_after_first_dispatched_message(self) -> None:
        _agent, record, sink, pulled = await _run_claude_communicate(stop_after=1, n_messages=3)
        # The deciding message is kept; the next is never pulled.
        assert pulled == 1
        assert record.crashed is False
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.STOPPED_EARLY
        assert ends[0].crashed is False

    async def test_early_stop_is_clean_not_crashed(self) -> None:
        agent, record, _sink, _pulled = await _run_claude_communicate(stop_after=1)
        # A clean stop: no partial pending_turn, no ERROR state, no raise (we got here).
        assert agent.pending_turn is None
        assert agent.get_state().value != "error"
        assert record.crashed is False

    async def test_should_stop_none_consumes_full_stream(self) -> None:
        _agent, _record, sink, pulled = await _run_claude_communicate(stop_after=None, n_messages=3)
        assert pulled == 3
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.COMPLETED

    async def test_should_stop_false_consumes_full_stream(self) -> None:
        _agent, _record, sink, pulled = await _run_claude_communicate(never=True, n_messages=3)
        assert pulled == 3
        assert _agent_end_events(sink)[0].status == AgentEndStatus.COMPLETED
