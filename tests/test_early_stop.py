"""Tests for early-stop-on-criterion (phases 1-3).

Phase 1 (config + contract): the two new opt-in config fields, the
``live_verdict`` / ``live_stop_polarities`` observability contract on the two
observable criteria, and ``validate_early_stop``'s guardrails on both the
``plan`` and ``run`` surfaces.

Phase 2 (agent seam): the cooperative ``should_stop`` seam on
``ClaudeCodeAgent.communicate`` and the ``STOPPED_EARLY`` status, plus the
timeout-beats-stop precedence (a deadline breach wins over a pending stop).

Phase 3 (feature live): the ``EarlyStopReason`` / ``EarlyStopInfo`` models and
the ``armed_criteria_passed`` gate; the ``EarlyStopWatcher`` runtime observer
(stop rule, fail-open, latching, attribution); and the orchestrator wiring
(watcher composed into the stream, ``result.early_stop`` populated, armed-subset
gate on an early-stopped run vs the full gate on a completed run).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.criteria import CriterionRegistry, init_criteria
from coder_eval.criteria.base import BaseCriterion
from coder_eval.criteria.command_executed import CommandExecutedChecker
from coder_eval.criteria.skill_triggered import SkillTriggeredChecker, _engaged_skill_names
from coder_eval.errors import TurnTimeoutError
from coder_eval.models import (
    AgentKind,
    CommandExecutedCriterion,
    CommandTelemetry,
    CriterionResult,
    EarlyStopInfo,
    EarlyStopReason,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    RunLimits,
    RunSummary,
    SandboxConfig,
    SimulationConfig,
    SkillTriggeredCriterion,
    TaskDefinition,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestration.early_stop import EarlyStopConfigError, EarlyStopWatcher, validate_early_stop
from coder_eval.orchestrator import Orchestrator, build_task_event
from coder_eval.reports import ReportGenerator
from coder_eval.reports_experiment import eval_result_to_task_dict
from coder_eval.reports_html import _render_criteria, _render_header
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    TurnEndStatus,
    TurnStartEvent,
)


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


# --- Phase-3 helpers: results / info / events ------------------------------ #


def _crit_result(ctype: str, score: float) -> CriterionResult:
    return CriterionResult(criterion_type=ctype, description=f"{ctype} result", score=score)


def _result(*, criteria_results: list[CriterionResult] | None = None) -> EvaluationResult:
    return EvaluationResult(
        task_id="r",
        task_description="d",
        agent_type="claude-code",
        started_at=_TS,
        final_status=FinalStatus.FAILURE,
        iteration_count=1,
        success_criteria_results=criteria_results or [],
    )


def _info(**overrides: Any) -> EarlyStopInfo:
    base: dict[str, Any] = dict(
        reason=EarlyStopReason.CRITERION_PASSED,
        deciding_criterion_type="skill_triggered",
        deciding_criterion_description="skill activation",
        armed_criteria=["skill_triggered: skill activation"],
        sdk_turn_index=1,
        tool_call_index=1,
        elapsed_seconds=1.0,
        turns_remaining_at_stop=14,
    )
    base.update(overrides)
    return EarlyStopInfo(**base)


def _agent_start() -> AgentStartEvent:
    return AgentStartEvent(task_id="t")


def _turn_start() -> TurnStartEvent:
    return TurnStartEvent(task_id="t")


def _skill_cmd(skill: str, *, tool_id: str) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name="Skill", tool_id=tool_id, timestamp=_TS, parameters={"skill": skill}, result_status="success"
    )


def _tool_end(cmd: CommandTelemetry) -> ToolEndEvent:
    return ToolEndEvent(task_id="t", tool=cmd)


def _skill_events(skill: str, *, tool_id: str = "sk-1") -> list[Any]:
    """AgentStart + TurnStart + a Skill ToolEnd engaging ``skill``."""
    return [_agent_start(), _turn_start(), _tool_end(_skill_cmd(skill, tool_id=tool_id))]


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


class _NoopWatchdog:
    """No-op stand-in for ThreadedWatchdog so only the in-loop deadline guard fires."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _NoopWatchdog:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


async def _run_claude_communicate_timeout() -> tuple[ClaudeCodeAgent, _EventSink, BaseException | None]:
    """Drive ``communicate`` with a slow query (50ms) against a 10ms deadline AND
    ``should_stop=True`` — the deadline guard must win. Returns
    ``(agent, sink, raised_exception)``."""
    config = parse_agent_config(type=AgentKind.CLAUDE_CODE, permission_mode="acceptEdits")
    agent = ClaudeCodeAgent(config)
    sink = _EventSink()
    raised: BaseException | None = None
    with tempfile.TemporaryDirectory() as tmpdir:
        await agent.start(tmpdir)

        async def slow_query(prompt: Any, options: Any, transport: Any = None) -> Any:
            await asyncio.sleep(0.05)
            yield _DummyMsg(0)

        with (
            patch("coder_eval.agents.claude_code_agent.query", slow_query),
            patch("coder_eval.agents.claude_code_agent.ThreadedWatchdog", _NoopWatchdog),
        ):
            try:
                await agent.communicate("p", stream_callback=sink, timeout=0.01, should_stop=lambda: True)
            except TurnTimeoutError as exc:
                raised = exc
    return agent, sink, raised


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

    async def test_timeout_beats_stop_precedence(self) -> None:
        # Both signals live in one turn: a deadline breach AND should_stop=True.
        # The top-of-loop deadline guard returns BEFORE dispatch, so the stop
        # check is never reached — TIMEOUT wins over the pending stop.
        agent, sink, raised = await _run_claude_communicate_timeout()
        assert isinstance(raised, TurnTimeoutError)
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.TIMEOUT
        assert ends[0].crashed is True
        # The crashed partial is preserved for the orchestrator to drain.
        assert agent.pending_turn is not None and agent.pending_turn.crashed is True
        # STOPPED_EARLY must NOT appear — the stop lost the race.
        assert AgentEndStatus.STOPPED_EARLY not in {e.status for e in ends}


# --------------------------------------------------------------------------- #
# Phase 3: EarlyStopReason / EarlyStopInfo / armed_criteria_passed
# --------------------------------------------------------------------------- #


class TestEarlyStopModels:
    def test_reason_values(self) -> None:
        assert EarlyStopReason.CRITERION_PASSED.value == "criterion_passed"
        assert EarlyStopReason.CRITERION_FAILED.value == "criterion_failed"

    def test_info_defaults(self) -> None:
        info = EarlyStopInfo(
            reason=EarlyStopReason.CRITERION_FAILED,
            deciding_criterion_type="command_executed",
            deciding_criterion_description="d",
            sdk_turn_index=2,
            tool_call_index=3,
            elapsed_seconds=1.5,
        )
        assert info.armed_criteria == []
        assert info.turns_remaining_at_stop is None

    def test_info_roundtrip(self) -> None:
        info = _info()
        assert EarlyStopInfo.model_validate_json(info.model_dump_json()) == info

    def test_evaluation_result_early_stop_defaults_none(self) -> None:
        assert _result().early_stop is None

    def test_evaluation_result_roundtrip_with_early_stop(self) -> None:
        result = _result(criteria_results=[_crit_result("skill_triggered", 1.0)])
        result.early_stop = _info()
        reloaded = EvaluationResult.model_validate_json(result.model_dump_json())
        assert reloaded.early_stop == _info()

    def test_armed_criteria_passed_gates_armed_only(self) -> None:
        # Armed skill passes; advisory file_exists fails. armed gate -> True.
        criteria = [
            _skill_crit("date-teller", "date-teller", stop_when="decided"),
            FileExistsCriterion(path="x", description="x must exist"),
        ]
        result = _result(criteria_results=[_crit_result("skill_triggered", 1.0), _crit_result("file_exists", 0.0)])
        assert result.armed_criteria_passed(criteria) is True
        # The full gate would (correctly) fail on the advisory 0.0.
        assert result.all_criteria_passed(criteria) is False

    def test_armed_criteria_passed_fails_when_armed_fails(self) -> None:
        criteria = [
            _skill_crit("date-teller", "date-teller", stop_when="decided"),
            FileExistsCriterion(path="x", description="x must exist"),
        ]
        result = _result(criteria_results=[_crit_result("skill_triggered", 0.0), _crit_result("file_exists", 1.0)])
        assert result.armed_criteria_passed(criteria) is False

    def test_armed_criteria_passed_raises_on_empty_armed(self) -> None:
        criteria = [FileExistsCriterion(path="x", description="x must exist")]
        result = _result(criteria_results=[_crit_result("file_exists", 1.0)])
        with pytest.raises(ValueError, match="no armed criteria"):
            result.armed_criteria_passed(criteria)


# --------------------------------------------------------------------------- #
# Phase 3: EarlyStopWatcher
# --------------------------------------------------------------------------- #


def _watcher(criteria: list[Any], *, max_turns: int | None = 20) -> EarlyStopWatcher:
    task = _task(criteria=criteria, stop_early=True)
    assert task.run_limits is not None
    task.run_limits.max_turns = max_turns
    return EarlyStopWatcher.for_task(task)


def _feed(watcher: EarlyStopWatcher, events: list[Any]) -> None:
    for event in events:
        watcher.on_event(event)


class TestEarlyStopWatcher:
    def test_for_task_arms_only_stop_criteria(self) -> None:
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="decided"),
                FileExistsCriterion(path="x", description="x must exist"),
            ]
        )
        # Only the armed criterion is tracked; the unarmed file_exists is ignored.
        assert len(watcher._armed) == 1

    def test_undecided_before_engagement_no_stop(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        _feed(watcher, [_agent_start(), _turn_start()])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_pass_stop_fires_on_expected_skill(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_fail_stop_fires_on_wrong_skill(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_pass_polarity_does_not_fire_on_fail(self) -> None:
        # stop_when="pass": a wrong-skill (live-fail) engagement must NOT stop.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is False

    def test_stacked_pass_stop(self) -> None:
        # Two armed skill criteria, both expecting date-teller; engaging it passes both.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="decided"),
                _skill_crit("weather-teller", "", stop_when="decided"),
            ]
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_stacked_wrong_skill_fail_stop(self) -> None:
        # Engaging weather-teller: date-teller row -> fail; the fail-stop fires first.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="decided"),
                _skill_crit("weather-teller", "date-teller", stop_when="decided"),
            ]
        )
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_records_turn_and_tool_index(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.sdk_turn_index == 1
        assert watcher.info.tool_call_index == 1

    def test_turns_remaining_from_max_turns(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")], max_turns=15)
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.turns_remaining_at_stop == 14  # 15 - sdk_turn_index(1)

    def test_turns_remaining_none_when_max_turns_unset(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")], max_turns=None)
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.turns_remaining_at_stop is None

    def test_fail_open_on_raising_verdict(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        with patch.object(SkillTriggeredChecker, "live_verdict", side_effect=RuntimeError("boom")):
            _feed(watcher, _skill_events("date-teller"))
        # Fail-open: disarmed, no false stop, degrades to a full run.
        assert watcher.disarmed is True
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_decision_latched_after_fire(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="decided")])
        _feed(watcher, _skill_events("date-teller"))
        fired = watcher.info
        # A subsequent (wrong-skill) engagement must not overwrite the latched decision.
        _feed(watcher, [_tool_end(_skill_cmd("weather-teller", tool_id="sk-2"))])
        assert watcher.info is fired
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED


# --------------------------------------------------------------------------- #
# Phase 3: Orchestrator wiring
# --------------------------------------------------------------------------- #


class _ScriptedAgent:
    """Duck-typed agent: replays scripted events through the callback, polling
    ``should_stop`` after each and breaking when it flips (mirrors the real
    message-boundary cut). Returns a fixed ``TurnRecord``."""

    def __init__(self, events: list[Any], turn: TurnRecord) -> None:
        self._events = events
        self._turn = turn
        self.pending_turn: TurnRecord | None = None
        self.delivered = 0

    def get_sdk_options(self) -> dict[str, Any] | None:
        return None

    async def communicate(
        self,
        prompt: str,
        *,
        stream_callback: Any = None,
        timeout: float | None = None,
        max_turns: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TurnRecord:
        for event in self._events:
            if stream_callback is not None:
                stream_callback.on_event(event)
            self.delivered += 1
            if should_stop is not None and should_stop():
                break
        return self._turn


async def _run_wiring(
    *,
    criteria: list[Any],
    events: list[Any],
    scores: list[float],
    stop_early: bool,
    tmp_path,
) -> tuple[EvaluationResult, _ScriptedAgent]:
    """Drive ``Orchestrator._evaluation_loop`` with a scripted agent + mock checker.

    ``scores`` are positional CriterionResult scores matching ``criteria``.
    The early-stop watcher is built directly (_setup is not invoked here).
    """
    task = _task(criteria=criteria, stop_early=stop_early)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="default")
    orch.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="default",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime.now(),
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
        environment_info={},
    )
    sandbox = MagicMock()
    sandbox.sandbox_dir = tmp_path / "sandbox"
    sandbox.sandbox_dir.mkdir()
    orch.sandbox = sandbox

    checker = MagicMock()
    checker.check_all = MagicMock(return_value=[_crit_result(c.type, s) for c, s in zip(criteria, scores, strict=True)])
    orch.success_checker = checker

    if stop_early:
        orch._early_stop_watcher = EarlyStopWatcher.for_task(task)

    turn = TurnRecord(iteration=1, user_input="p", agent_output="done")
    agent = _ScriptedAgent(events, turn)
    orch.agent = agent  # type: ignore[assignment]

    with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
        await orch._evaluation_loop()
    assert orch.result is not None
    return orch.result, agent


class TestOrchestratorEarlyStopWiring:
    _SKILL = "date-teller"

    def _criteria(self, *, expected: str = "date-teller", stop_when: str | None = "decided") -> list[Any]:
        # Armed skill_triggered + advisory file_exists (deliberately failing).
        return [
            _skill_crit(self._SKILL, expected, stop_when=stop_when),
            FileExistsCriterion(path="artifact.txt", description="artifact must exist"),
        ]

    async def test_default_off_full_gate_no_early_stop(self, tmp_path) -> None:
        # Unarmed: no watcher, all criteria gate, advisory 0.0 drags to FAILURE.
        result, agent = await _run_wiring(
            criteria=self._criteria(stop_when=None),
            events=_skill_events(self._SKILL),
            scores=[1.0, 0.0],
            stop_early=False,
            tmp_path=tmp_path,
        )
        assert result.early_stop is None
        assert agent.delivered == 3  # full stream consumed (should_stop=None)

    async def test_pass_stop_cuts_the_stream(self, tmp_path) -> None:
        # A trailing event AFTER the deciding ToolEnd proves the cut: delivered == 3.
        events = [*_skill_events(self._SKILL), _turn_start()]
        result, agent = await _run_wiring(
            criteria=self._criteria(),
            events=events,
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert agent.delivered == 3
        assert result.early_stop is not None
        assert result.early_stop.reason == EarlyStopReason.CRITERION_PASSED

    async def test_fail_stop_wiring(self, tmp_path) -> None:
        result, _agent = await _run_wiring(
            criteria=self._criteria(),
            events=_skill_events("weather-teller"),
            scores=[0.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is not None
        assert result.early_stop.reason == EarlyStopReason.CRITERION_FAILED

    async def test_early_stop_info_fields_populated(self, tmp_path) -> None:
        result, _agent = await _run_wiring(
            criteria=self._criteria(),
            events=_skill_events(self._SKILL),
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is not None
        assert result.early_stop.sdk_turn_index == 1
        assert result.early_stop.tool_call_index == 1
        assert result.early_stop.deciding_criterion_type == "skill_triggered"

    async def test_advisory_not_gated_on_early_stop(self, tmp_path) -> None:
        # Armed skill passes (1.0), advisory file_exists fails (0.0): armed gate -> SUCCESS.
        result, _agent = await _run_wiring(
            criteria=self._criteria(),
            events=_skill_events(self._SKILL),
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is not None
        assert result.all_criteria_passed(self._criteria()) is False  # full gate would fail
        assert result.armed_criteria_passed(self._criteria()) is True  # armed gate passes

    async def test_completed_naturally_uses_full_gate(self, tmp_path) -> None:
        # Armed, but the skill is never engaged -> watcher never fires -> full gate,
        # so the advisory 0.0 legitimately drags the completed run to FAILURE.
        result, agent = await _run_wiring(
            criteria=self._criteria(),
            events=[_agent_start(), _turn_start()],  # no skill engagement
            scores=[0.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is None
        assert agent.delivered == 2  # full (short) stream consumed
        assert result.all_criteria_passed(self._criteria()) is False

    async def test_fail_open_wiring_degrades_to_full_run(self, tmp_path) -> None:
        with patch.object(SkillTriggeredChecker, "live_verdict", side_effect=RuntimeError("boom")):
            result, _agent = await _run_wiring(
                criteria=self._criteria(),
                events=_skill_events(self._SKILL),
                scores=[1.0, 0.0],
                stop_early=True,
                tmp_path=tmp_path,
            )
        # Fail-open: no early_stop recorded, full gate applies.
        assert result.early_stop is None


# --------------------------------------------------------------------------- #
# Report / telemetry surfaces
# --------------------------------------------------------------------------- #


def _stopped_result(
    *,
    reason: EarlyStopReason = EarlyStopReason.CRITERION_PASSED,
    turns_remaining: int | None = 14,
    criteria_results: list[CriterionResult] | None = None,
) -> EvaluationResult:
    result = _result(criteria_results=criteria_results)
    result.early_stop = _info(reason=reason, turns_remaining_at_stop=turns_remaining)
    return result


def _run_summary(task_dicts: list[dict[str, Any]]) -> RunSummary:
    # framework_version is a required RunSummary field; the count invariant needs
    # succeeded + failed + error == tasks_run.
    return RunSummary(
        run_id="r",
        start_time=_TS,
        end_time=_TS,
        total_duration_seconds=0.0,
        tasks_run=len(task_dicts),
        tasks_succeeded=len(task_dicts),
        tasks_failed=0,
        tasks_error=0,
        task_results=task_dicts,
        framework_version="test",
    )


class TestEarlyStopReportSurfaces:
    def test_task_dict_keys_present_when_early_stopped(self) -> None:
        d = eval_result_to_task_dict(_stopped_result())
        assert d["stopped_early"] is True
        assert d["early_stop_reason"] == "criterion_passed"
        assert d["turns_remaining_at_stop"] == 14

    def test_task_dict_keys_defaulted_when_not_early_stopped(self) -> None:
        d = eval_result_to_task_dict(_result())
        assert d["stopped_early"] is False
        assert d["early_stop_reason"] is None
        assert d["turns_remaining_at_stop"] is None

    def test_runtime_note_rendered_with_turns_avoided(self) -> None:
        lines = ReportGenerator._runtime_notes_lines(_run_summary([eval_result_to_task_dict(_stopped_result())]))
        blob = "\n".join(lines)
        assert "stopped early (criterion_passed)" in blob
        assert "<= 14 turn(s) avoided" in blob
        assert "gated on armed criteria only; other criteria are advisory" in blob

    def test_runtime_note_absent_for_unarmed_run(self) -> None:
        lines = ReportGenerator._runtime_notes_lines(_run_summary([eval_result_to_task_dict(_result())]))
        assert not any("stopped early" in line for line in lines)

    def test_html_header_shows_early_stop_badge(self) -> None:
        html = _render_header(_stopped_result())
        assert "stopped early (criterion_passed)" in html
        # No badge on a normal run.
        assert "stopped early" not in _render_header(_result())

    def test_html_criteria_marks_only_advisory_rows(self) -> None:
        # Armed skill_triggered (matches _info.armed_criteria) + advisory file_exists.
        armed = _crit_result("skill_triggered", 1.0)
        armed.description = "skill activation"  # match the armed_criteria key format
        advisory = _crit_result("file_exists", 0.0)
        result = _stopped_result(criteria_results=[armed, advisory])
        html = _render_criteria(result.success_criteria_results, result.early_stop)
        assert html.count("advisory — not gated (run stopped early)") == 1
        # A completed (non-early-stopped) run gets no advisory markers at all.
        assert "advisory — not gated" not in _render_criteria([armed, advisory], None)

    def test_telemetry_dims_reflect_early_stop(self) -> None:
        _name, props = build_task_event(_stopped_result(), driver="tempdir", variant_id="v")
        assert props["EarlyStopped"] is True
        assert props["EarlyStopReason"] == "criterion_passed"
        # Defaulted on a normal run.
        _n2, props2 = build_task_event(_result(), driver="tempdir", variant_id="v")
        assert props2["EarlyStopped"] is False
        assert props2["EarlyStopReason"] == ""
