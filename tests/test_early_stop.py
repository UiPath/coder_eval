"""Tests for early-stop-on-criterion (phases 1-3).

Phase 1 (config + contract): the two new opt-in config fields, the
``live_verdict`` / ``LiveSuccessCriterion`` observability contract on the two
observable criteria, and ``validate_early_stop``'s guardrails on both the
``plan`` and ``run`` surfaces.

Phase 2 (agent seam): the cooperative ``should_stop`` seam on
``ClaudeCodeAgent.communicate`` and the ``STOPPED_EARLY`` status, plus the
timeout-beats-stop precedence (a deadline breach wins over a pending stop).
The same seam contract on ``CodexAgent`` and ``AntigravityAgent`` (stop cuts
the stream, clean STOPPED_EARLY finalize, timeout precedence, and the
post-stop-exception guard that prevents retry poisoning) is covered at the
end of this file.

Phase 3 (feature live): the ``EarlyStopReason`` / ``EarlyStopInfo`` models and
the ``armed_criteria_passed`` gate; the ``EarlyStopWatcher`` runtime observer
(stop rule, fail-open, latching, attribution); and the orchestrator wiring
(watcher composed into the stream, ``result.early_stop`` populated, armed-subset
gate on an early-stopped run vs the full gate on a completed run).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from coder_eval.agents.antigravity_agent import AntigravityAgent
from coder_eval.agents.claude_code_agent import ClaudeCodeAgent
from coder_eval.agents.codex_agent import CodexAgent, _CodexTurnState
from coder_eval.agents.registry import AgentRegistry
from coder_eval.cli.plan_command import plan_command
from coder_eval.criteria import CriterionRegistry, init_criteria
from coder_eval.criteria.command_executed import CommandExecutedChecker
from coder_eval.criteria.skill_triggered import SkillTriggeredChecker, _engaged_skill_names
from coder_eval.errors import AgentCrashError, TurnTimeoutError
from coder_eval.models import (
    AgentKind,
    BaseAgentConfig,
    CommandExecutedCriterion,
    CommandTelemetry,
    CriterionResult,
    EarlyStopInfo,
    EarlyStopReason,
    EvaluationResult,
    ExperimentDefinition,
    ExperimentVariant,
    FileExistsCriterion,
    FinalStatus,
    LiveSuccessCriterion,
    RunLimits,
    RunSummary,
    SandboxConfig,
    SimulationConfig,
    SkillTriggeredCriterion,
    TaskDefinition,
    TurnRecord,
    parse_agent_config,
)
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.early_stop import EarlyStopConfigError, EarlyStopWatcher, validate_early_stop
from coder_eval.orchestration.experiment import resolve_all_tasks
from coder_eval.orchestrator import Orchestrator, build_task_event
from coder_eval.reports import ReportGenerator
from coder_eval.reports_experiment import eval_result_to_task_dict
from coder_eval.reports_html import _render_criteria, _render_header
from coder_eval.streaming.events import (
    AgentEndEvent,
    AgentEndStatus,
    AgentStartEvent,
    ToolEndEvent,
    ToolEndStatus,
    ToolStartEvent,
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
    agent_type: AgentKind | str = AgentKind.CLAUDE_CODE,
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


class _DummyNoStopConfig(BaseAgentConfig):
    """Config for the dummy non-supporting agent registered by the fixture below."""


class _DummyNoStopAgent:
    """Agent stand-in that leaves ``supports_cooperative_stop`` at the default False.

    ``validate_early_stop`` only reads the flag off the registered class, so no
    ``Agent`` machinery is needed. Guardrail 1 must keep rejecting agents that
    have not opted into the cooperative interrupt (all built-ins now support it).
    """

    supports_cooperative_stop = False


@pytest.fixture
def dummy_no_stop_kind() -> Iterator[str]:
    """Register a non-supporting agent kind for guardrail-1 tests, then clean up."""
    kind = "dummy-no-stop"
    AgentRegistry.register(kind, _DummyNoStopConfig)(_DummyNoStopAgent)
    try:
        yield kind
    finally:
        AgentRegistry._registry.pop(kind, None)


def _skill_crit(
    skill_name: str,
    expected_skill: str,
    *,
    stop_when: str | None = None,
    weight: float = 1.0,
    max_steps_to_decide: int | None = None,
    pass_threshold: float = 0.9,
) -> SkillTriggeredCriterion:
    return SkillTriggeredCriterion(
        type="skill_triggered",
        description=f"{skill_name} activation",
        skill_name=skill_name,
        expected_skill=expected_skill,
        stop_when=stop_when,  # type: ignore[arg-type]
        weight=weight,
        max_steps_to_decide=max_steps_to_decide,
        pass_threshold=pass_threshold,
    )


def _cmd_crit(
    *,
    min_count: int = 1,
    max_count: int | None = None,
    pattern: str | None = "curl",
    stop_when: str | None = None,
    weight: float = 1.0,
    max_steps_to_decide: int | None = None,
) -> CommandExecutedCriterion:
    return CommandExecutedCriterion(
        type="command_executed",
        description="command check",
        tool_name="Bash",
        command_pattern=pattern,
        min_count=min_count,
        max_count=max_count,
        stop_when=stop_when,  # type: ignore[arg-type]
        weight=weight,
        max_steps_to_decide=max_steps_to_decide,
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


def _skill_start(skill: str, *, tool_id: str = "sk-1", sequence_number: int = 0) -> ToolStartEvent:
    """A Skill ToolStart (the tool CALL) engaging ``skill`` — no result yet."""
    cmd = CommandTelemetry(
        tool_name="Skill",
        tool_id=tool_id,
        timestamp=_TS,
        parameters={"skill": skill},
        sequence_number=sequence_number,
    )
    return ToolStartEvent(task_id="t", tool=cmd)


def _skill_events(skill: str, *, tool_id: str = "sk-1") -> list[Any]:
    """AgentStart + TurnStart + a Skill ToolEnd engaging ``skill``."""
    return [_agent_start(), _turn_start(), _tool_end(_skill_cmd(skill, tool_id=tool_id))]


def _unresolved_skill_end(skill: str, *, tool_id: str = "orphan-1") -> ToolEndEvent:
    """An orphan-closing ToolEnd (status UNRESOLVED), as finalize() emits."""
    return ToolEndEvent(task_id="t", tool=_skill_cmd(skill, tool_id=tool_id), status=ToolEndStatus.UNRESOLVED)


# --------------------------------------------------------------------------- #
# Config surface
# --------------------------------------------------------------------------- #


class TestConfigSurface:
    def test_stop_early_defaults_false(self) -> None:
        assert RunLimits().stop_early is False

    def test_stop_early_settable(self) -> None:
        assert RunLimits(stop_early=True).stop_early is True

    def test_gate_threshold_out_of_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="less than or equal to 1"):
            RunLimits(stop_early=True, stop_early_gate_threshold=1.5)
        with pytest.raises(ValueError, match="greater than or equal to 0"):
            RunLimits(stop_early=True, stop_early_gate_threshold=-0.1)

    def test_gate_threshold_nondefault_without_stop_early_rejected(self) -> None:
        # A non-default threshold is a silent no-op unless stop_early is also
        # True (the orchestrator only reads it inside the early_stop-fired
        # branch) — reject it as a dead-config hard error, not a silent no-op.
        with pytest.raises(ValueError, match="stop_early is False"):
            RunLimits(stop_early=False, stop_early_gate_threshold=0.7)

    def test_gate_threshold_zero_with_stop_early_rejected(self) -> None:
        # A threshold of exactly 0 trivially satisfies the pass-stop floor and
        # the final weighted gate regardless of whether anything decided —
        # neutralizing the armed pass/fail gate outright.
        with pytest.raises(ValueError, match=r"must be > 0\.0"):
            RunLimits(stop_early=True, stop_early_gate_threshold=0.0)

    def test_gate_threshold_default_is_valid_either_way(self) -> None:
        assert RunLimits(stop_early=False).stop_early_gate_threshold == 1.0
        assert RunLimits(stop_early=True).stop_early_gate_threshold == 1.0

    def test_stop_when_defaults_none(self) -> None:
        assert _skill_crit("s", "s").stop_when is None

    @pytest.mark.parametrize("value", ["pass", "fail", "decided", "auto"])
    def test_stop_when_accepts_valid_polarities(self, value: str) -> None:
        assert _skill_crit("s", "s", stop_when=value).stop_when == value

    def test_stop_when_rejects_invalid_polarity(self) -> None:
        with pytest.raises(ValueError):
            _skill_crit("s", "s", stop_when="maybe")

    def test_stop_when_auto_roundtrips(self) -> None:
        # The new `auto` value survives model_dump -> model_validate with its
        # model_fields_set intact (Pydantic round-trip integrity).
        crit = _skill_crit("s", "s", stop_when="auto")
        restored = SkillTriggeredCriterion.model_validate_json(crit.model_dump_json())
        assert restored.stop_when == "auto"
        assert "stop_when" in restored.model_fields_set

    def test_max_steps_to_decide_defaults_none(self) -> None:
        assert _skill_crit("s", "s", stop_when="pass").max_steps_to_decide is None

    def test_max_steps_to_decide_requires_stop_when(self) -> None:
        with pytest.raises(ValueError, match="max_steps_to_decide requires stop_when"):
            _skill_crit("s", "s", max_steps_to_decide=5)

    def test_max_steps_to_decide_allowed_with_stop_when(self) -> None:
        crit = _skill_crit("s", "s", stop_when="pass", max_steps_to_decide=5)
        assert crit.max_steps_to_decide == 5

    def test_max_steps_to_decide_requires_stop_when_command_executed(self) -> None:
        # The other LiveSuccessCriterion subclass — same validator, same error.
        with pytest.raises(ValueError, match="max_steps_to_decide requires stop_when"):
            _cmd_crit(max_steps_to_decide=5)


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

    def test_windows_file_read_path_segment(self) -> None:
        got = _engaged_skill_names(_cmd("Read", {"file_path": r"C:\repo\skills\date-teller\SKILL.md"}))
        assert got == {"date-teller"}

    def test_json_escaped_windows_path_segment(self) -> None:
        got = _engaged_skill_names(_cmd("Bash", {"command": r"type C:\\repo\\skills\\date-teller\\SKILL.md"}))
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

    def test_positive_undecided_when_only_wrong_skill_engaged(self) -> None:
        # A positive row expecting date-teller, but a different skill loads. Under
        # any-engagement the positive criterion does NOT fail — date-teller may
        # still load later, so the verdict stays undecided (the run keeps going).
        crit = _skill_crit("date-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "other-skill"}))]
        assert self.checker.live_verdict(crit, rec) == "undecided"

    def test_distractor_fails_when_its_skill_engaged(self) -> None:
        # A distractor criterion (skill_name != expected_skill): engaging its
        # (wrong) skill is a decidable precision miss -> fail.
        crit = _skill_crit("weather-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "weather-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_negative_row_target_engaged_is_fail(self) -> None:
        # expected_skill == "" (negative): engaging the target skill fails.
        crit = _skill_crit("date-teller", "")
        rec = [_turn(_cmd("Skill", {"skill": "date-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "fail"

    def test_negative_undecided_when_other_engaged(self) -> None:
        # A negative criterion cannot live-pass: the absence of its skill is not
        # knowable mid-run, so an unrelated engagement leaves it undecided.
        crit = _skill_crit("date-teller", "")
        rec = [_turn(_cmd("Skill", {"skill": "unrelated"}))]
        assert self.checker.live_verdict(crit, rec) == "undecided"

    def test_expected_skill_engaged_after_wrong_is_pass(self) -> None:
        # Item 1: the wrong skill engages first, the expected one later — the
        # positive criterion passes (any-engagement, order-independent).
        crit = _skill_crit("date-teller", "date-teller")
        rec = [_turn(_cmd("Skill", {"skill": "wrong"}), _cmd("Skill", {"skill": "date-teller"}))]
        assert self.checker.live_verdict(crit, rec) == "pass"

    def test_is_live_success_criterion(self) -> None:
        assert isinstance(_skill_crit("date-teller", "date-teller"), LiveSuccessCriterion)

    def test_decidable_narrows_per_instance(self) -> None:
        # A positive instance decides only pass; a distractor/negative only fail.
        assert _skill_crit("date-teller", "date-teller").live_decidable_polarities() == frozenset({"pass"})
        assert _skill_crit("weather-teller", "date-teller").live_decidable_polarities() == frozenset({"fail"})
        assert _skill_crit("date-teller", "").live_decidable_polarities() == frozenset({"fail"})


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

    def test_is_live_success_criterion(self) -> None:
        assert isinstance(_cmd_crit(min_count=1), LiveSuccessCriterion)

    def test_decidable_pass_only_when_no_upper_bound(self) -> None:
        crit = _cmd_crit(min_count=1, max_count=None)
        assert crit.live_decidable_polarities() == frozenset({"pass"})

    def test_decidable_fail_only_when_upper_bound_set(self) -> None:
        crit = _cmd_crit(min_count=1, max_count=3)
        assert crit.live_decidable_polarities() == frozenset({"fail"})

    def test_decidable_fail_for_must_not_run(self) -> None:
        crit = _cmd_crit(min_count=0, max_count=0)
        assert crit.live_decidable_polarities() == frozenset({"fail"})

    def test_decidable_empty_for_zero_min_no_max(self) -> None:
        # min_count=0 + no upper bound: neither pass nor fail can ever fire.
        crit = _cmd_crit(min_count=0, max_count=None)
        assert crit.live_decidable_polarities() == frozenset()

    def test_decidable_is_subset_of_type_universe(self) -> None:
        # The instance set can never exceed {"pass", "fail"} — the type's universe.
        for min_c, max_c in [(1, None), (1, 3), (0, 0), (0, None)]:
            crit = _cmd_crit(min_count=min_c, max_count=max_c)
            assert crit.live_decidable_polarities() <= frozenset({"pass", "fail"})


# --------------------------------------------------------------------------- #
# Base default: unobservable criteria
# --------------------------------------------------------------------------- #


class TestBaseLiveVerdictDefault:
    def test_unobservable_criterion_is_not_a_live_success_criterion(self) -> None:
        # file_exists is not observable mid-run: its model is plain
        # BaseSuccessCriterion, not LiveSuccessCriterion — no
        # live_decidable_polarities method to call at all.
        crit = FileExistsCriterion(type="file_exists", path="x.txt", description="x")
        assert not isinstance(crit, LiveSuccessCriterion)

    def test_unobservable_checker_is_undecided(self) -> None:
        init_criteria(validate=False)
        checker = CriterionRegistry.get_checker("file_exists")()
        crit = FileExistsCriterion(type="file_exists", path="x.txt", description="x")
        assert checker.live_verdict(crit, [_turn()]) == "undecided"

    def test_skill_triggered_decidable_is_subset_of_type_universe(self) -> None:
        # skill_triggered narrows per-instance; each instance set stays a
        # subset of the type's universe ({"pass", "fail"}).
        for crit in (_skill_crit("s", "s"), _skill_crit("s", "other"), _skill_crit("s", "")):
            assert crit.live_decidable_polarities() <= frozenset({"pass", "fail"})


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
        # A positive skill_triggered decides only "pass", so arm it with pass.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="pass")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_armed_distractor_fail_accepts(self) -> None:
        # A distractor (skill_name != expected_skill) decides only "fail".
        task = _task(criteria=[_skill_crit("wrong", "s", stop_when="fail")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_skill_triggered_positive_fail_arm_rejected(self) -> None:
        # A positive criterion can never live-fail; arming it with fail is a dead arm.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="fail")], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_skill_triggered_decided_arm_rejected(self) -> None:
        # A single skill_triggered instance decides only one polarity, so
        # stop_when=decided (which needs both) can never be honored.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_armed_command_executed_accepts(self) -> None:
        # A decidable fail arm: must-NOT-run (max_count set) can live-fail.
        task = _task(criteria=[_cmd_crit(stop_when="fail", min_count=0, max_count=0)], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_armed_command_executed_pass_accepts(self) -> None:
        # A decidable pass arm: min_count>0 with no upper bound can live-pass.
        task = _task(criteria=[_cmd_crit(stop_when="pass", min_count=1, max_count=None)], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_dead_arm_pass_with_max_count_rejected(self) -> None:
        # stop_when=pass but max_count is set → live pass can never fire.
        task = _task(criteria=[_cmd_crit(stop_when="pass", min_count=1, max_count=3)], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_dead_arm_fail_without_max_count_rejected(self) -> None:
        # stop_when=fail but max_count is None → live fail can never fire.
        task = _task(criteria=[_cmd_crit(stop_when="fail", min_count=1, max_count=None)], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_dead_arm_zero_min_no_max_rejected(self) -> None:
        # min_count=0, max_count=None → neither polarity can ever fire (empty set).
        task = _task(criteria=[_cmd_crit(stop_when="decided", min_count=0, max_count=None)], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_dead_arm_decided_with_max_count_rejected(self) -> None:
        # stop_when=decided needs BOTH polarities; max_count set gives only fail.
        task = _task(criteria=[_cmd_crit(stop_when="decided", min_count=1, max_count=3)], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="cannot decide polarity"):
            validate_early_stop(task)

    def test_guardrail5_simulation_rejected(self) -> None:
        sim = SimulationConfig(enabled=True, persona="user", goal="get it done")
        task = _task(criteria=[_skill_crit("s", "s", stop_when="decided")], stop_early=True, simulation=sim)
        with pytest.raises(EarlyStopConfigError, match="simulation"):
            validate_early_stop(task)

    def test_guardrail1_non_supporting_agent_rejected(self, dummy_no_stop_kind: str) -> None:
        # Codex/antigravity now support the cooperative interrupt, so guardrail 1
        # is exercised with a dummy agent that leaves the flag at False.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="pass")], stop_early=True, agent_type=dummy_no_stop_kind)
        with pytest.raises(EarlyStopConfigError, match="cooperative stopping"):
            validate_early_stop(task)

    def test_guardrail1_armed_codex_accepts(self) -> None:
        task = _task(criteria=[_skill_crit("s", "s", stop_when="pass")], stop_early=True, agent_type=AgentKind.CODEX)
        validate_early_stop(task)  # no raise

    def test_guardrail1_armed_antigravity_accepts(self) -> None:
        task = _task(
            criteria=[_skill_crit("s", "s", stop_when="pass")], stop_early=True, agent_type=AgentKind.ANTIGRAVITY
        )
        validate_early_stop(task)  # no raise

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
        monkeypatch.setattr(CommandExecutedCriterion, "live_decidable_polarities", lambda self: frozenset({"pass"}))
        task = _task(criteria=[_cmd_crit(stop_when="fail")], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="polarity"):
            validate_early_stop(task)

    def test_raise_order_simulation_before_agent(self, dummy_no_stop_kind: str) -> None:
        # Both simulation AND a non-supporting agent are invalid; simulation reports first.
        sim = SimulationConfig(enabled=True, persona="user", goal="g")
        task = _task(
            criteria=[_skill_crit("s", "s", stop_when="decided")],
            stop_early=True,
            agent_type=dummy_no_stop_kind,
            simulation=sim,
        )
        with pytest.raises(EarlyStopConfigError, match="simulation"):
            validate_early_stop(task)

    def test_stacked_activation_criteria_accept(self) -> None:
        # The activation pattern under the any-engagement latch: the positive (GT)
        # criterion arms pass, a distractor arms fail. `decided` is invalid for
        # either because a single instance decides only one polarity.
        crits = [
            _skill_crit("skill-a", "skill-a", stop_when="pass"),  # positive -> pass
            _skill_crit("skill-b", "skill-a", stop_when="fail"),  # distractor -> fail
        ]
        task = _task(criteria=crits, stop_early=True)
        validate_early_stop(task)  # no raise

    def test_auto_positive_accepts(self) -> None:
        # `auto` on a positive resolves to the pass polarity it can decide.
        task = _task(criteria=[_skill_crit("s", "s", stop_when="auto")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_auto_distractor_accepts(self) -> None:
        # `auto` on a distractor resolves to the fail polarity it can decide.
        task = _task(criteria=[_skill_crit("wrong", "s", stop_when="auto")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_auto_negative_row_distractor_accepts(self) -> None:
        # A negative row's criterion (expected_skill == "") is a distractor -> fail.
        task = _task(criteria=[_skill_crit("wrong", "", stop_when="auto")], stop_early=True)
        validate_early_stop(task)  # no raise

    def test_auto_stacked_activation_accepts(self) -> None:
        # The real activation shape: ONE uniform `stop_when: auto` across every
        # stacked criterion, which resolves per-instance to pass (the positive) or
        # fail (each distractor). This is what a single fanned-out `stop_when` value
        # can express and `pass`/`fail`/`decided` cannot, since the role flips per row.
        crits = [
            _skill_crit("skill-a", "skill-a", stop_when="auto"),  # positive -> pass
            _skill_crit("skill-b", "skill-a", stop_when="auto"),  # distractor -> fail
            _skill_crit("skill-c", "skill-a", stop_when="auto"),  # distractor -> fail
        ]
        task = _task(criteria=crits, stop_early=True)
        validate_early_stop(task)  # no raise

    def test_auto_dead_arm_rejected(self) -> None:
        # `auto` on an instance that can decide NEITHER polarity is a dead arm and
        # must be rejected, not silently degrade to a full run. command_executed
        # with min_count=0 + max_count=None supports no live polarity.
        task = _task(criteria=[_cmd_crit(stop_when="auto", min_count=0, max_count=None)], stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="no polarity"):
            validate_early_stop(task)


# --------------------------------------------------------------------------- #
# Guardrail integration: the plan and run resolution surfaces actually invoke
# validate_early_stop (not just the helper in isolation). Real task YAMLs go
# through the real load + 5-layer merge; a bad arming must surface as a clean
# CLI-level error on BOTH surfaces, never a silent no-op.
# --------------------------------------------------------------------------- #

_ARMED_UNOBSERVABLE_CRITERION = """\
  - type: file_exists
    description: out exists
    path: out.txt
    stop_when: pass
"""

_ARMED_OBSERVABLE_CRITERION = """\
  - type: skill_triggered
    description: date-teller activation
    skill_name: date-teller
    expected_skill: date-teller
    stop_when: pass
"""


def _write_task_yaml(tmp_path: Path, *, criterion_yaml: str, stop_early: bool) -> Path:
    task_file = tmp_path / "es_task.yaml"
    task_file.write_text(
        "task_id: es-guardrail-task\n"
        + "description: early-stop guardrail surface test\n"
        + "initial_prompt: do the thing\n"
        + "agent:\n"
        + "  type: claude-code\n"
        + "sandbox:\n"
        + "  driver: tempdir\n"
        + "run_limits:\n"
        + "  max_turns: 20\n"
        + f"  stop_early: {str(stop_early).lower()}\n"
        + "success_criteria:\n"
        + criterion_yaml
    )
    return task_file


def _resolve_surface(task_file: Path, tmp_path: Path, *, overrides: dict[str, Any] | None = None):
    """Drive the real run-surface resolution (load + 5-layer merge + guardrails)."""
    single_variant = [ExperimentVariant(variant_id="default")]
    return resolve_all_tasks(
        task_files=[task_file],
        experiment=ExperimentDefinition(experiment_id="exp", variants=single_variant),
        default_experiment=ExperimentDefinition(experiment_id="default", variants=single_variant),
        config=BatchRunConfig(run_dir=tmp_path / "runs", overrides=overrides or {}),
    )


class TestGuardrailResolutionSurfaces:
    """A bad arming is rejected by the real plan/run wiring, not only the helper."""

    def test_run_surface_rejects_bad_armed_task(self, tmp_path: Path) -> None:
        # The armed unobservable criterion propagates out of resolve_all_tasks as
        # EarlyStopConfigError (a ValueError, so the run CLI converts it to a
        # clean BadParameter) instead of being demoted to a skipped task.
        task_file = _write_task_yaml(tmp_path, criterion_yaml=_ARMED_UNOBSERVABLE_CRITERION, stop_early=True)
        with pytest.raises(EarlyStopConfigError, match="observable"):
            _resolve_surface(task_file, tmp_path)

    def test_run_surface_accepts_valid_armed_task(self, tmp_path: Path) -> None:
        task_file = _write_task_yaml(tmp_path, criterion_yaml=_ARMED_OBSERVABLE_CRITERION, stop_early=True)
        resolved, skipped = _resolve_surface(task_file, tmp_path)
        assert not skipped
        assert len(resolved) == 1
        limits = resolved[0].task.run_limits
        assert limits is not None and limits.stop_early is True

    def test_run_surface_validates_cli_override_arming(self, tmp_path: Path) -> None:
        # The YAML alone is inert (stop_when set, stop_early false) and must be
        # accepted; arming via the layer-5 -D override must then be validated,
        # proving the guardrails run AFTER _apply_cli_overrides.
        task_file = _write_task_yaml(tmp_path, criterion_yaml=_ARMED_UNOBSERVABLE_CRITERION, stop_early=False)
        resolved, _ = _resolve_surface(task_file, tmp_path)
        assert len(resolved) == 1  # inert without the override
        with pytest.raises(EarlyStopConfigError, match="observable"):
            _resolve_surface(task_file, tmp_path, overrides={"run_limits.stop_early": True})

    def _run_plan(self, task_file: Path, exp_dir: Path) -> tuple[str, int]:
        """Invoke the real plan_command against a minimal single-variant experiment.

        Returns the concatenated console output and the exit code (0 when plan
        returned normally).
        """
        exp_file = exp_dir / "es_experiment.yaml"
        exp_file.write_text("experiment_id: es-guardrail\nvariants:\n  - variant_id: default\n")
        exit_code = 0
        with (
            patch("coder_eval.cli.plan_command.check_tools"),
            patch("coder_eval.cli.plan_command.check_api_keys"),
            # Point the default-experiment lookup at a nonexistent file so plan
            # falls back to the explicit experiment (hermetic vs. the repo tree).
            patch("coder_eval.orchestration.experiment.DEFAULT_EXPERIMENT_PATH", exp_dir / "missing.yaml"),
            patch("coder_eval.cli.plan_command.console") as mock_console,
        ):
            try:
                plan_command(task_files=[task_file], experiment=exp_file)
            except typer.Exit as exc:
                exit_code = exc.exit_code
        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        return printed, exit_code

    def test_plan_surface_flips_exit_code_on_bad_armed_task(self, tmp_path: Path) -> None:
        task_file = _write_task_yaml(tmp_path, criterion_yaml=_ARMED_UNOBSERVABLE_CRITERION, stop_early=True)
        printed, exit_code = self._run_plan(task_file, tmp_path)
        assert exit_code == 1
        assert "early-stop config error" in printed
        assert "observable" in printed

    def test_plan_surface_accepts_valid_armed_task(self, tmp_path: Path) -> None:
        task_file = _write_task_yaml(tmp_path, criterion_yaml=_ARMED_OBSERVABLE_CRITERION, stop_early=True)
        printed, exit_code = self._run_plan(task_file, tmp_path)
        assert exit_code == 0
        assert "All tasks are valid!" in printed


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


class TestNewFixtureTasksResolve:
    """Cheap resolution-only coverage for the 3 checked-in early-stop example
    task YAMLs — no live agent involved. These are deliberately NOT tagged
    smoke-pass/smoke-fail (their pass/fail outcome depends on non-deterministic
    agent behavior), so this is their only pre-merge signal that a malformed
    weight/stop_when/max_steps_to_decide combo would otherwise slip through.
    """

    @pytest.mark.parametrize(
        "task_file",
        [
            Path("tasks/early_stop_weighted_low_weight_absorbed.yaml"),
            Path("tasks/early_stop_weighted_high_weight_kills_run.yaml"),
            Path("tasks/early_stop_decision_budget_exceeded.yaml"),
        ],
    )
    def test_fixture_resolves_without_error(self, task_file: Path, tmp_path: Path) -> None:
        resolved, skipped = _resolve_surface(task_file, tmp_path)
        assert not skipped
        assert len(resolved) == 1
        limits = resolved[0].task.run_limits
        assert limits is not None and limits.stop_early is True


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
            _skill_crit("date-teller", "date-teller", stop_when="pass"),
            FileExistsCriterion(path="x", description="x must exist"),
        ]
        result = _result(criteria_results=[_crit_result("skill_triggered", 1.0), _crit_result("file_exists", 0.0)])
        assert result.armed_criteria_passed(criteria) is True
        # The full gate would (correctly) fail on the advisory 0.0.
        assert result.all_criteria_passed(criteria) is False

    def test_armed_criteria_passed_fails_when_armed_fails(self) -> None:
        criteria = [
            _skill_crit("date-teller", "date-teller", stop_when="pass"),
            FileExistsCriterion(path="x", description="x must exist"),
        ]
        result = _result(criteria_results=[_crit_result("skill_triggered", 0.0), _crit_result("file_exists", 1.0)])
        assert result.armed_criteria_passed(criteria) is False

    def test_armed_criteria_passed_raises_on_empty_armed(self) -> None:
        criteria = [FileExistsCriterion(path="x", description="x must exist")]
        result = _result(criteria_results=[_crit_result("file_exists", 1.0)])
        with pytest.raises(ValueError, match="no armed criteria"):
            result.armed_criteria_passed(criteria)

    def test_armed_criteria_passed_default_threshold_still_requires_all(self) -> None:
        # gate_threshold=1.0 (the default) must reproduce the old all()-must-pass
        # rule exactly: one armed criterion at 0.0 fails the gate regardless of
        # the other armed criterion's weight.
        criteria = [
            _skill_crit("date-teller", "date-teller", stop_when="pass", weight=0.8),
            _skill_crit("weather-teller", "date-teller", stop_when="fail", weight=0.2),
        ]
        low_weight_fails = _result(
            criteria_results=[_crit_result("skill_triggered", 1.0), _crit_result("skill_triggered", 0.0)]
        )
        assert low_weight_fails.armed_criteria_passed(criteria) is False
        high_weight_fails = _result(
            criteria_results=[_crit_result("skill_triggered", 0.0), _crit_result("skill_triggered", 1.0)]
        )
        assert high_weight_fails.armed_criteria_passed(criteria) is False
        all_pass = _result(
            criteria_results=[_crit_result("skill_triggered", 1.0), _crit_result("skill_triggered", 1.0)]
        )
        assert all_pass.armed_criteria_passed(criteria) is True

    def test_armed_criteria_passed_low_weight_failure_absorbed_below_threshold(self) -> None:
        # The user's worked example: weights 0.8/0.2, gate_threshold 0.7. The
        # LOW-weight criterion failing (weighted score 0.8) still clears 0.7;
        # the HIGH-weight one failing (weighted score 0.2) does not.
        criteria = [
            _skill_crit("date-teller", "date-teller", stop_when="pass", weight=0.8),
            _skill_crit("weather-teller", "date-teller", stop_when="fail", weight=0.2),
        ]
        low_weight_fails = _result(
            criteria_results=[_crit_result("skill_triggered", 1.0), _crit_result("skill_triggered", 0.0)]
        )
        assert low_weight_fails.armed_criteria_passed(criteria, gate_threshold=0.7) is True
        high_weight_fails = _result(
            criteria_results=[_crit_result("skill_triggered", 0.0), _crit_result("skill_triggered", 1.0)]
        )
        assert high_weight_fails.armed_criteria_passed(criteria, gate_threshold=0.7) is False

    def test_armed_criteria_passed_ignores_pass_threshold(self) -> None:
        # armed_criteria_passed no longer consults each criterion's own
        # pass_threshold at all — only the weighted average vs gate_threshold.
        # A score that would FAIL this criterion's own pass_threshold (0.99)
        # still passes the armed gate once gate_threshold is lowered to match
        # it — proving pass_threshold plays no role in the weighted-average
        # math. Currently safe only because the two live-observable criteria
        # always score binary 0.0/1.0 in practice (0.5 here is a synthetic
        # score to exercise the boundary); pin this with a regression test so
        # a future fractional LiveSuccessCriterion can't silently reintroduce
        # wrong gating.
        criteria = [_skill_crit("date-teller", "date-teller", stop_when="pass", pass_threshold=0.99)]
        result = _result(criteria_results=[_crit_result("skill_triggered", 0.5)])
        assert result.armed_criteria_passed(criteria, gate_threshold=0.5) is True

    def test_armed_criteria_passed_weighted_gate_with_command_executed(self) -> None:
        # The other LiveSuccessCriterion subclass exercised through the same
        # weighted gate — command_executed, not just skill_triggered.
        criteria = [
            _cmd_crit(min_count=1, max_count=None, stop_when="pass", weight=0.8),
            _cmd_crit(min_count=0, max_count=0, stop_when="fail", weight=0.2),
        ]
        low_weight_fails = _result(
            criteria_results=[_crit_result("command_executed", 1.0), _crit_result("command_executed", 0.0)]
        )
        assert low_weight_fails.armed_criteria_passed(criteria, gate_threshold=0.7) is True
        high_weight_fails = _result(
            criteria_results=[_crit_result("command_executed", 0.0), _crit_result("command_executed", 1.0)]
        )
        assert high_weight_fails.armed_criteria_passed(criteria, gate_threshold=0.7) is False


# --------------------------------------------------------------------------- #
# Phase 3: EarlyStopWatcher
# --------------------------------------------------------------------------- #


def _watcher(criteria: list[Any], *, max_turns: int | None = 20, gate_threshold: float = 1.0) -> EarlyStopWatcher:
    task = _task(criteria=criteria, stop_early=True)
    assert task.run_limits is not None
    task.run_limits.max_turns = max_turns
    task.run_limits.stop_early_gate_threshold = gate_threshold
    return EarlyStopWatcher.for_task(task)


def _feed(watcher: EarlyStopWatcher, events: list[Any]) -> None:
    for event in events:
        watcher.on_event(event)


class TestEarlyStopWatcher:
    def test_for_task_arms_only_stop_criteria(self) -> None:
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass"),
                FileExistsCriterion(path="x", description="x must exist"),
            ]
        )
        # Only the armed criterion is tracked; the unarmed file_exists is ignored.
        assert len(watcher._armed) == 1

    def test_undecided_before_engagement_no_stop(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start()])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_pass_stop_fires_on_expected_skill(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_fail_stop_fires_on_distractor_skill(self) -> None:
        # A distractor criterion (its skill != the expected skill) fail-stops the
        # instant its skill is engaged — the per-skill precision signal.
        watcher = _watcher([_skill_crit("weather-teller", "date-teller", stop_when="fail")])
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_wrong_skill_does_not_stop_positive_row(self) -> None:
        # Item 1: a positive row (armed pass) engaging the WRONG skill must NOT
        # stop — the run keeps going so the expected skill can still load later.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_stacked_pass_stop_requires_all(self) -> None:
        # Pass-stop needs EVERY armed criterion to live-pass. Two positives for
        # different skills: engaging only the first does not stop; engaging the
        # second (both now passed) fires the pass-stop.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass"),
                _skill_crit("weather-teller", "weather-teller", stop_when="pass"),
            ]
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is False  # only one of two has passed
        _feed(watcher, [_tool_end(_skill_cmd("weather-teller", tool_id="w"))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_stacked_wrong_skill_defers_fail_stop_until_positive_decides(self) -> None:
        # The recall guard: a positive (armed pass) + a distractor (armed fail).
        # The distractor misfiring FIRST must NOT stop — cutting here would freeze
        # the would-be TP as an FN and deflate suite recall. The misfire is latched
        # by the criterion's monotone semantics, so once the expected skill engages
        # (no pass-armed criterion left undecided) the deferred fail-stop fires.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass"),
                _skill_crit("weather-teller", "date-teller", stop_when="fail"),
            ]
        )
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is False  # positive undecided -> fail deferred
        assert watcher.info is None
        _feed(watcher, [_tool_end(_skill_cmd("date-teller", tool_id="d"))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED
        assert watcher.info.deciding_criterion_description == "weather-teller activation"

    def test_fail_stop_precedes_pass_stop_same_round(self) -> None:
        # Precedence pin (kills the block-swap mutation): ONE tool call engages
        # both the expected skill and a distractor via file reads, so the positive
        # live-passes and the distractor live-fails in the SAME evaluation round
        # with no pass-armed criterion left undecided. Fail-stop is evaluated
        # before pass-stop, so the round must record CRITERION_FAILED.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="auto"),  # positive -> pass
                _skill_crit("weather-teller", "date-teller", stop_when="auto"),  # distractor -> fail
            ]
        )
        both = _cmd("Bash", {"command": "cat skills/date-teller/SKILL.md skills/weather-teller/SKILL.md"})
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(both)])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED
        assert watcher.info.deciding_criterion_description == "weather-teller activation"

    def test_auto_positive_row_misfire_alone_never_stops(self) -> None:
        # A positive row armed `auto` whose agent only ever touches wrong skills:
        # the fail-stop stays deferred for the whole run (the positive never
        # decides), so the run continues to the cap and full-trajectory scoring —
        # never a truncated FN.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="auto"),  # positive -> pass
                _skill_crit("weather-teller", "date-teller", stop_when="auto"),  # distractor -> fail
            ]
        )
        _feed(watcher, _skill_events("weather-teller"))
        _feed(watcher, [_turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_auto_positive_pass_stops(self) -> None:
        # `auto` on a positive resolves to pass-armed: engaging the expected skill
        # pass-stops, identically to an explicit stop_when="pass".
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="auto")])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_auto_mixed_pass_stops_ignoring_undecided_distractors(self) -> None:
        # THE mixed-arming fix: one positive + two distractors, all armed `auto`.
        # Engaging ONLY the expected skill pass-stops on turn 1 even though the two
        # distractors are still "undecided" — fail-armed criteria are not required
        # to live-pass. (Under the old "every armed must pass" rule this could never
        # fire, since a distractor can never live-pass.)
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="auto"),  # positive -> pass
                _skill_crit("weather-teller", "date-teller", stop_when="auto"),  # distractor -> fail
                _skill_crit("news-teller", "date-teller", stop_when="auto"),  # distractor -> fail
            ]
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED
        # The deciding criterion is the positive that flipped to pass.
        assert watcher.info.deciding_criterion_description == "date-teller activation"

    def test_auto_negative_row_no_pass_stop_on_benign_call(self) -> None:
        # THE vacuous guard: a negative row (expected_skill == "") stacks only
        # distractors, so there are ZERO pass-armed criteria. A benign non-skill
        # tool call must NOT pass-stop on turn 0 (empty all() would be vacuously
        # True); the run continues to the cap as intended.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "", stop_when="auto"),  # distractor -> fail
                _skill_crit("weather-teller", "", stop_when="auto"),  # distractor -> fail
            ]
        )
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_auto_negative_row_misfire_fail_stops(self) -> None:
        # The other half of the asymmetry: a negative row that DOES engage a skill
        # is a misfire and fail-stops (the precision signal), even though it can
        # never pass-stop.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "", stop_when="auto"),  # distractor -> fail
                _skill_crit("weather-teller", "", stop_when="auto"),  # distractor -> fail
            ]
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_mixed_static_arming_pass_stops_ignoring_fail_armed(self) -> None:
        # The pass-armed-subset rule is not `auto`-specific: an explicit
        # pass-positive + fail-distractor mix also pass-stops on the positive alone.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass"),  # pass-armed
                _skill_crit("weather-teller", "date-teller", stop_when="fail"),  # fail-armed
            ]
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_ceiling_bound_defers_fail_stop_below_default_gate_threshold(self) -> None:
        # The user's worked example on the trigger side: weights 0.8/0.2,
        # gate_threshold 0.7. The LOW-weight (0.2) criterion misfiring leaves a
        # ceiling of 0.8 (>= 0.7) — the gate could still pass if the high-weight
        # positive comes through, so the run must NOT stop yet.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="auto", weight=0.8),
                _skill_crit("weather-teller", "date-teller", stop_when="auto", weight=0.2),
            ],
            gate_threshold=0.7,
        )
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_ceiling_bound_fires_fail_stop_when_high_weight_criterion_fails(self) -> None:
        # Mirror case: the HIGH-weight (0.8) positive misfiring as a distractor
        # leaves a ceiling of 0.2 (< 0.7) — the gate can never reach 0.7 no
        # matter what the low-weight criterion does, so the fail-stop must fire
        # even though it's the "small" criterion still undecided.
        watcher = _watcher(
            [
                _skill_crit("weather-teller", "date-teller", stop_when="auto", weight=0.8),
                _skill_crit("news-teller", "date-teller", stop_when="auto", weight=0.2),
            ],
            gate_threshold=0.7,
        )
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_default_gate_threshold_fires_fail_stop_on_any_weight(self) -> None:
        # At the default gate_threshold=1.0, even the low-weight criterion's
        # failure alone must still fire — byte-for-byte the pre-weighting rule.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="auto", weight=0.8),
                _skill_crit("weather-teller", "date-teller", stop_when="auto", weight=0.2),
            ]
        )
        _feed(watcher, _skill_events("weather-teller"))
        assert watcher.should_stop() is False  # deferred: positive still undecided
        _feed(watcher, [_tool_end(_skill_cmd("date-teller", tool_id="d"))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_floor_bound_pass_stops_before_low_weight_distractor_decides(self) -> None:
        # Floor generalization on the pass side: a high-weight (0.9) positive
        # pass-armed criterion passing is enough to pass-stop on its own —
        # there is no OTHER pass-armed criterion whose weight it needs to share
        # the floor with (fail-armed distractors are excluded from the
        # pass-armed floor by design either way).
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass", weight=0.9),
            ],
            gate_threshold=0.7,
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_floor_bound_pass_stop_requires_full_pass_armed_subset_below_default(self) -> None:
        # Below the default threshold, a partially-decided pass-armed subset
        # (one of two passed) must NOT pass-stop yet if the still-undecided
        # one's weight share would drop the floor below the threshold.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass", weight=0.5),
                _skill_crit("weather-teller", "weather-teller", stop_when="pass", weight=0.5),
            ],
            gate_threshold=0.7,
        )
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_decision_budget_exceeded_when_still_undecided(self) -> None:
        # An armed criterion capped at max_steps_to_decide=1 that is still
        # "undecided" after its first tool call forces a budget-exceeded stop.
        # Full-field EarlyStopInfo parity, matching every other stop-reason test.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass", max_steps_to_decide=1)])
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.DECISION_BUDGET_EXCEEDED
        assert watcher.info.deciding_criterion_type == "skill_triggered"
        assert watcher.info.deciding_criterion_description == "date-teller activation"
        assert watcher.info.sdk_turn_index == 1
        assert watcher.info.tool_call_index == 1

    def test_decision_budget_exceeded_names_the_right_criterion_among_several(self) -> None:
        # Multiple armed criteria with different budgets: only the SECOND
        # one's budget has expired (cap=1, undecided after 1 call); the first
        # has a longer budget (cap=5) and is also still undecided. The
        # deciding criterion reported must be the one whose budget actually
        # tripped, not just the first armed criterion in list order.
        watcher = _watcher(
            [
                _skill_crit("date-teller", "date-teller", stop_when="pass", max_steps_to_decide=5),
                _skill_crit("weather-teller", "weather-teller", stop_when="pass", max_steps_to_decide=1),
            ]
        )
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.DECISION_BUDGET_EXCEEDED
        assert watcher.info.deciding_criterion_description == "weather-teller activation"

    def test_decision_budget_exceeded_with_command_executed(self) -> None:
        # The other LiveSuccessCriterion subclass: a command_executed pass-armed
        # criterion (min_count=1, no upper bound) capped at max_steps_to_decide=1
        # that never sees a matching command force-fails identically.
        watcher = _watcher([_cmd_crit(min_count=1, max_count=None, stop_when="pass", max_steps_to_decide=1)])
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.DECISION_BUDGET_EXCEEDED
        assert watcher.info.deciding_criterion_type == "command_executed"

    def test_decision_budget_not_exceeded_below_cap(self) -> None:
        # Same cap, but only reached on the FIRST tool call (index 1) — a cap of
        # 2 must not fire yet.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass", max_steps_to_decide=2)])
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_real_decision_within_budget_wins_over_budget_check(self) -> None:
        # The criterion decides (pass-stops) on the SAME tool call that would
        # otherwise have tripped its budget — the real decision takes priority.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass", max_steps_to_decide=1)])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_decision_budget_ignored_when_unset(self) -> None:
        # No max_steps_to_decide -> no budget check, run continues indefinitely
        # (up to run_limits.max_turns) while undecided.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))])
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_records_turn_and_tool_index(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.sdk_turn_index == 1
        assert watcher.info.tool_call_index == 1

    def test_turns_remaining_from_max_turns(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")], max_turns=15)
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.turns_remaining_at_stop == 14  # 15 - sdk_turn_index(1)

    def test_turns_remaining_none_when_max_turns_unset(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")], max_turns=None)
        _feed(watcher, _skill_events("date-teller"))
        assert watcher.info is not None
        assert watcher.info.turns_remaining_at_stop is None

    def test_fail_open_on_raising_verdict(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        with patch.object(SkillTriggeredChecker, "live_verdict", side_effect=RuntimeError("boom")):
            _feed(watcher, _skill_events("date-teller"))
        # Fail-open: disarmed, no false stop, degrades to a full run.
        assert watcher.disarmed is True
        assert watcher.should_stop() is False
        assert watcher.info is None

    def test_unresolved_tool_end_does_not_latch(self) -> None:
        # finalize() force-closes orphaned tools as UNRESOLVED AFTER the message
        # loop ends and the terminal status is chosen. Such an orphan Skill
        # engagement must NOT trip a stop, else a naturally-completed (or
        # timed-out / crashed) run gets recorded as early-stopped.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start(), _unresolved_skill_end("date-teller")])
        assert watcher.should_stop() is False
        assert watcher.info is None
        assert watcher._tool_call_index == 0  # the unresolved end is not even counted

    def test_resolved_after_unresolved_still_decides(self) -> None:
        # An UNRESOLVED end is dropped, but a later RESOLVED engagement still fires
        # the stop (dropping orphans never suppresses a real, observed stop).
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start(), _unresolved_skill_end("date-teller")])
        assert watcher.info is None
        _feed(watcher, [_tool_end(_skill_cmd("date-teller", tool_id="sk-real"))])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_decision_latched_after_fire(self) -> None:
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, _skill_events("date-teller"))
        fired = watcher.info
        # A subsequent (wrong-skill) engagement must not overwrite the latched decision.
        _feed(watcher, [_tool_end(_skill_cmd("weather-teller", tool_id="sk-2"))])
        assert watcher.info is fired
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_tool_call_fires_before_result(self) -> None:
        # The decision latches on the tool CALL (ToolStartEvent): a Skill call
        # whose result never arrives (a cut-short turn would strip it) still stops.
        # No ToolEndEvent is ever fed.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start(), _skill_start("date-teller")])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED
        # The in-flight call reports as the 1st tool call even without a ToolEnd.
        assert watcher.info.tool_call_index == 1

    def test_tool_call_distractor_fail_fires(self) -> None:
        # A distractor (armed fail) fail-stops on the tool CALL that engages its
        # skill, before any result arrives.
        watcher = _watcher([_skill_crit("weather-teller", "date-teller", stop_when="fail")])
        _feed(watcher, [_agent_start(), _turn_start(), _skill_start("weather-teller")])
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_FAILED

    def test_tool_call_latches_on_file_read_engagement(self) -> None:
        # Off-Claude agents (antigravity/codex) engage a skill by READING its files
        # (skills/<name>/...), not via a Skill tool call. The watcher must latch on
        # that Read ToolStart — the file-path parameter carries the signal on the
        # call itself, so early-stop fires off-Claude just as it does for Claude.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        read = CommandTelemetry(
            tool_name="Read",
            tool_id="r1",
            timestamp=_TS,
            parameters={"file_path": "/repo/skills/date-teller/SKILL.md"},
        )
        _feed(watcher, [_agent_start(), _turn_start(), ToolStartEvent(task_id="t", tool=read)])
        assert watcher.should_stop() is True
        assert watcher.info is not None
        assert watcher.info.reason == EarlyStopReason.CRITERION_PASSED

    def test_tool_call_latches_before_unresolved_end(self) -> None:
        # The call fires the stop in-loop; a later finalize() UNRESOLVED end for
        # the SAME call is short-circuited (decision already latched) — no relabel,
        # no double count.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start(), _turn_start(), _skill_start("date-teller", tool_id="sk-1")])
        fired = watcher.info
        _feed(watcher, [_unresolved_skill_end("date-teller", tool_id="sk-1")])
        assert watcher.info is fired
        assert watcher.info is not None
        assert watcher.info.tool_call_index == 1

    def test_tool_call_index_counts_prior_resolved_calls(self) -> None:
        # A prior resolved, non-deciding tool is counted at its ToolEnd; the
        # deciding in-flight call is then reported as the next (2nd) call.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        prior = _cmd("Bash", {"command": "ls"})  # not a skill engagement
        _feed(watcher, [_agent_start(), _turn_start(), _tool_end(prior)])
        assert watcher.info is None
        _feed(watcher, [_skill_start("date-teller", tool_id="sk-1", sequence_number=1)])
        assert watcher.info is not None
        assert watcher.info.tool_call_index == 2

    def test_second_agent_start_does_not_reset_origin(self) -> None:
        # The wall-clock origin is stamped at the FIRST AgentStartEvent only; a
        # retry's second AgentStart must NOT reset it (the documented no-op branch
        # in on_event). Exercised deterministically via _started_monotonic rather
        # than the time-based elapsed_seconds field.
        watcher = _watcher([_skill_crit("date-teller", "date-teller", stop_when="pass")])
        _feed(watcher, [_agent_start()])
        origin = watcher._started_monotonic
        assert origin is not None
        # A second AgentStart (as on a retry) must leave the origin untouched.
        _feed(watcher, [_agent_start(), _turn_start()])
        assert watcher._started_monotonic == origin
        # The stop that follows anchors elapsed_seconds to that first origin.
        _feed(watcher, [_skill_start("date-teller")])
        assert watcher.info is not None
        assert watcher.info.elapsed_seconds >= 0.0


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
    agent_type: AgentKind = AgentKind.CLAUDE_CODE,
) -> tuple[EvaluationResult, _ScriptedAgent]:
    """Drive ``Orchestrator._evaluation_loop`` with a scripted agent + mock checker.

    ``scores`` are positional CriterionResult scores matching ``criteria``.
    The early-stop watcher is built directly (_setup is not invoked here).
    """
    task = _task(criteria=criteria, stop_early=stop_early, agent_type=agent_type)
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="default")
    orch.result = EvaluationResult(
        task_id=task.task_id,
        task_description=task.description,
        variant_id="default",
        agent_type=agent_type,
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
    checker.check_all_async = AsyncMock(
        return_value=[_crit_result(c.type, s) for c, s in zip(criteria, scores, strict=True)]
    )
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

    def _criteria(self, *, expected: str = "date-teller", stop_when: str | None = "pass") -> list[Any]:
        # Armed positive skill_triggered + advisory file_exists (deliberately failing).
        return [
            _skill_crit(self._SKILL, expected, stop_when=stop_when),
            FileExistsCriterion(path="artifact.txt", description="artifact must exist"),
        ]

    def _distractor_criteria(self) -> list[Any]:
        # A distractor (armed fail) + advisory file_exists, for the fail-stop path.
        return [
            _skill_crit("weather-teller", self._SKILL, stop_when="fail"),
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
        # A distractor (armed fail) fires the fail-stop when its skill is engaged.
        result, _agent = await _run_wiring(
            criteria=self._distractor_criteria(),
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

    async def test_decision_budget_exceeded_forces_failure_bypassing_gate(self, tmp_path) -> None:
        # A criterion capped at max_steps_to_decide=1 that never engages its
        # skill forces a hard fail — even though BOTH mocked criterion scores
        # are 1.0 (the weighted gate, if consulted, would pass).
        criteria = [
            _skill_crit(self._SKILL, self._SKILL, stop_when="pass", max_steps_to_decide=1),
            FileExistsCriterion(path="artifact.txt", description="artifact must exist"),
        ]
        task = _task(criteria=criteria, stop_early=True)
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
        checker.check_all_async = AsyncMock(return_value=[_crit_result(c.type, 1.0) for c in criteria])
        orch.success_checker = checker
        orch._early_stop_watcher = EarlyStopWatcher.for_task(task)
        turn = TurnRecord(iteration=1, user_input="p", agent_output="done")
        events = [_agent_start(), _turn_start(), _tool_end(_cmd("Bash", {"command": "echo hi"}))]
        agent = _ScriptedAgent(events, turn)
        orch.agent = agent  # type: ignore[assignment]

        with patch("coder_eval.orchestrator.load_reference", return_value=(None, None, None)):
            success = await orch._evaluation_loop()

        assert orch.result.early_stop is not None
        assert orch.result.early_stop.reason == EarlyStopReason.DECISION_BUDGET_EXCEEDED
        assert success is False

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

    async def test_completed_run_with_orphan_tool_not_early_stopped(self, tmp_path) -> None:
        # Regression: a run that completes naturally, whose finalize() force-closes
        # an orphaned Skill call as UNRESOLVED, must NOT be recorded as
        # early-stopped — the full gate applies and the advisory 0.0 drags to
        # FAILURE (rather than a false "stopped early; N turns avoided").
        events = [_agent_start(), _turn_start(), _unresolved_skill_end(self._SKILL)]
        result, agent = await _run_wiring(
            criteria=self._criteria(),
            events=events,
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is None
        assert agent.delivered == 3  # never stopped: the full stream was consumed
        assert result.all_criteria_passed(self._criteria()) is False

    async def test_tool_call_cut_without_tool_end(self, tmp_path) -> None:
        # End-to-end: the deciding Skill CALL (a ToolStart with no ToolEnd) cuts
        # the stream and records an early stop — the case that would otherwise run
        # to the turn cap when a cut-short turn strips the result.
        events = [_agent_start(), _turn_start(), _skill_start(self._SKILL), _turn_start()]
        result, agent = await _run_wiring(
            criteria=self._criteria(),
            events=events,
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
        )
        assert result.early_stop is not None
        assert result.early_stop.reason == EarlyStopReason.CRITERION_PASSED
        # Cut at the ToolStart: the trailing turn_start is never delivered.
        assert agent.delivered == 3

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


# --------------------------------------------------------------------------- #
# Cooperative should_stop seam on CodexAgent — mirrors TestCooperativeStopSeam.
# SDK-independent: the pump is driven over fake notifications (agentMessage
# deltas need no openai_codex types) and turn/completed handling is stubbed.
# --------------------------------------------------------------------------- #


class _CodexNotifIter:
    """Counting iterator over fake notifications (the pump pulls via ``next``)."""

    def __init__(self, notifications: list[Any]) -> None:
        self._it = iter(notifications)
        self.pulled = 0

    def __iter__(self) -> _CodexNotifIter:
        return self

    def __next__(self) -> Any:
        item = next(self._it)
        self.pulled += 1
        return item


class _FakeCodexStream:
    def __init__(self, notifications: list[Any]) -> None:
        self.iter = _CodexNotifIter(notifications)
        self.closed = False

    def __iter__(self) -> _CodexNotifIter:
        return self.iter

    def close(self) -> None:
        self.closed = True


class _FakeCodexTurnHandle:
    def __init__(self, stream: _FakeCodexStream) -> None:
        self._stream = stream
        self.interrupts = 0

    def stream(self) -> _FakeCodexStream:
        return self._stream

    def interrupt(self) -> None:
        self.interrupts += 1


def _codex_delta(i: int) -> SimpleNamespace:
    """A fake ``item/agentMessage/delta`` notification (no SDK types involved)."""
    return SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(delta=f"chunk{i} ", item=None))


def _codex_completed() -> SimpleNamespace:
    """A fake ``turn/completed`` notification, consumed by the stubbed handler."""
    return SimpleNamespace(method="turn/completed", payload=SimpleNamespace(items=None))


def _stub_on_turn_completed(self: Any, notification: Any) -> bool:
    """Stand-in for ``_CodexTurnState.on_turn_completed`` (the real one isinstance-
    checks an openai_codex type). Sets the terminal turn and breaks the pump."""
    self.turn_result = notification.payload
    return True


def _codex_agent() -> CodexAgent:
    agent = CodexAgent(parse_agent_config(type=AgentKind.CODEX, model="gpt-5-codex"))  # type: ignore[arg-type]
    agent.working_directory = Path("/tmp")
    agent.codex_client = SimpleNamespace()  # truthy: communicate()'s started check
    return agent


async def _run_codex_communicate(
    *,
    notifications: list[Any],
    stop_after: int | None = None,
    never: bool = False,
    timeout: float | None = None,
) -> tuple[CodexAgent, TurnRecord, _EventSink, _FakeCodexStream, _FakeCodexTurnHandle]:
    """Drive ``CodexAgent.communicate`` over a fake notification stream.

    ``stop_after``: should_stop returns True once that many notifications have
    been pulled (checked after each dispatch). ``never``: an always-False
    should_stop. Neither: ``should_stop=None``.
    """
    agent = _codex_agent()
    stream = _FakeCodexStream(notifications)
    handle = _FakeCodexTurnHandle(stream)
    agent.thread = SimpleNamespace(turn=lambda _prompt: handle)

    should_stop: Callable[[], bool] | None
    if stop_after is not None:
        should_stop = lambda: stream.iter.pulled >= stop_after  # noqa: E731
    elif never:
        should_stop = lambda: False  # noqa: E731
    else:
        should_stop = None

    sink = _EventSink()
    with patch.object(_CodexTurnState, "on_turn_completed", _stub_on_turn_completed):
        record = await agent.communicate("prompt", stream_callback=sink, timeout=timeout, should_stop=should_stop)
    return agent, record, sink, stream, handle


class TestCodexCooperativeStopSeam:
    async def test_stop_after_first_dispatched_notification(self) -> None:
        notifications = [_codex_delta(0), _codex_delta(1), _codex_delta(2), _codex_completed()]
        agent, record, sink, stream, handle = await _run_codex_communicate(notifications=notifications, stop_after=1)
        # The deciding notification is kept; the next is never pulled.
        assert stream.iter.pulled == 1
        # The in-flight turn was interrupted exactly once (server-side spend cut).
        assert handle.interrupts == 1
        assert record.crashed is False
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.STOPPED_EARLY
        assert ends[0].crashed is False
        # A clean stop: no partial pending_turn, no ERROR state, no raise.
        assert agent.pending_turn is None
        assert agent.get_state().value != "error"

    async def test_should_stop_none_consumes_full_stream(self) -> None:
        notifications = [_codex_delta(0), _codex_delta(1), _codex_completed()]
        _agent, record, sink, stream, handle = await _run_codex_communicate(notifications=notifications)
        assert stream.iter.pulled == 3
        assert handle.interrupts == 0
        assert record.crashed is False
        assert _agent_end_events(sink)[0].status == AgentEndStatus.COMPLETED

    async def test_should_stop_false_consumes_full_stream(self) -> None:
        notifications = [_codex_delta(0), _codex_delta(1), _codex_completed()]
        _agent, _record, sink, stream, _handle = await _run_codex_communicate(notifications=notifications, never=True)
        assert stream.iter.pulled == 3
        assert _agent_end_events(sink)[0].status == AgentEndStatus.COMPLETED

    async def test_stop_before_turn_completed_does_not_raise(self) -> None:
        # The stream is cut before any turn/completed: turn_result is None, but the
        # stop makes the "turn never completed" raise conditional — no crash.
        notifications = [_codex_delta(0), _codex_delta(1), _codex_delta(2)]
        _agent, record, sink, _stream, _handle = await _run_codex_communicate(notifications=notifications, stop_after=1)
        assert record.crashed is False
        assert _agent_end_events(sink)[0].status == AgentEndStatus.STOPPED_EARLY

    async def test_stream_dying_without_stop_still_raises(self) -> None:
        # Regression guard: a stream that ends with NO turn/completed and NO stop
        # is still a crash (the RuntimeError survives for genuine stream deaths).
        agent = _codex_agent()
        stream = _FakeCodexStream([_codex_delta(0)])
        agent.thread = SimpleNamespace(turn=lambda _prompt: _FakeCodexTurnHandle(stream))
        with pytest.raises(AgentCrashError, match="did not complete"):
            await agent.communicate("prompt", stream_callback=_EventSink(), should_stop=None)
        assert agent.pending_turn is not None
        assert agent.pending_turn.crashed is True
        await agent.discard_pending_turn()

    async def test_timeout_beats_stop_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both signals in one turn: the watchdog fires (timeout_hit) AND should_stop
        # is True. The post-pump timeout check must win — TIMEOUT, crashed=True.
        class _FiringWatchdog:
            def __init__(self, *, on_timeout: Callable[[], None], **_kwargs: Any) -> None:
                self._on_timeout = on_timeout

            def __enter__(self) -> _FiringWatchdog:
                self._on_timeout()  # watchdog fired: state.timeout_hit = True
                return self

            def __exit__(self, *_exc: Any) -> bool:
                return False

        monkeypatch.setattr("coder_eval.agents.codex_agent.ThreadedWatchdog", _FiringWatchdog)
        agent = _codex_agent()
        stream = _FakeCodexStream([_codex_delta(0), _codex_delta(1)])
        agent.thread = SimpleNamespace(turn=lambda _prompt: _FakeCodexTurnHandle(stream))
        sink = _EventSink()
        with pytest.raises(TurnTimeoutError):
            await agent.communicate("prompt", stream_callback=sink, timeout=30.0, should_stop=lambda: True)
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.TIMEOUT
        assert ends[0].crashed is True
        assert agent.pending_turn is not None and agent.pending_turn.crashed is True
        # STOPPED_EARLY must NOT appear — the stop lost the race.
        assert AgentEndStatus.STOPPED_EARLY not in {e.status for e in ends}
        await agent.discard_pending_turn()

    async def test_post_stop_exception_stays_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The retry-poisoning gap: an exception AFTER the cooperative break (here:
        # the pump's finally-side cleanup) must NOT crash-finalize the turn — a
        # crash would trigger the orchestrator retry with the watcher's decision
        # still latched, stopping the retry at turn 0.
        def _boom(self: Any) -> None:
            raise RuntimeError("post-stop cleanup boom")

        monkeypatch.setattr(_CodexTurnState, "close_open_tools", _boom)
        notifications = [_codex_delta(0), _codex_delta(1)]
        agent, record, sink, _stream, _handle = await _run_codex_communicate(notifications=notifications, stop_after=1)
        # No AgentCrashError raised (we got a record back), clean STOPPED_EARLY.
        assert record.crashed is False
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.STOPPED_EARLY
        assert ends[0].crashed is False
        assert agent.pending_turn is None

    async def test_post_stop_cleanup_exception_without_stop_still_crashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard is scoped to stopped turns only: the same cleanup exception on
        # a NON-stopped turn keeps crashing (no behavior change for real failures).
        def _boom(self: Any) -> None:
            raise RuntimeError("cleanup boom")

        monkeypatch.setattr(_CodexTurnState, "close_open_tools", _boom)
        agent = _codex_agent()
        stream = _FakeCodexStream([_codex_delta(0)])
        agent.thread = SimpleNamespace(turn=lambda _prompt: _FakeCodexTurnHandle(stream))
        with pytest.raises(AgentCrashError):
            await agent.communicate("prompt", stream_callback=_EventSink(), should_stop=None)
        await agent.discard_pending_turn()

    async def test_stopped_turn_skips_subagent_recovery(self) -> None:
        # A stopped turn must not attempt rollout recovery: children may have no
        # rollout yet and the run is already decided.
        agent = _codex_agent()
        stream = _FakeCodexStream([_codex_delta(0), _codex_delta(1)])
        agent.thread = SimpleNamespace(turn=lambda _prompt: _FakeCodexTurnHandle(stream))
        recover = AsyncMock()
        captured: dict[str, Any] = {}

        original_init = _CodexTurnState.__init__

        def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self.spawned_children = [("child-thread", "tool-1", None)]
            captured["state"] = self

        with (
            patch.object(_CodexTurnState, "__init__", _capturing_init),
            patch.object(CodexAgent, "_recover_subagent_tool_calls", recover),
        ):
            await agent.communicate("prompt", stream_callback=_EventSink(), should_stop=lambda: stream.iter.pulled >= 1)
        assert captured["state"].stopped_early_hit is True
        recover.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Cooperative should_stop seam on AntigravityAgent — same contract, driven over
# a fake step stream (mirrors tests/test_antigravity_agent.py's conventions).
# --------------------------------------------------------------------------- #


def _ag_step(i: int) -> SimpleNamespace:
    """A minimal streamed text step (plain strings stand in for the SDK enums)."""
    return SimpleNamespace(
        type="TEXT_RESPONSE",
        status="ACTIVE",
        source="MODEL",
        target="TARGET_USER",
        tool_calls=[],
        content="",
        content_delta=f"c{i}",
        thinking="",
        thinking_delta="",
        usage_metadata=None,
        is_complete_response=None,
        error="",
        step_index=i,
    )


class _CountingConversation:
    def __init__(self, steps: list[Any], *, cancel_raises: bool = False) -> None:
        self._steps = steps
        self._cancel_raises = cancel_raises
        self.yielded = 0
        self.cancels = 0
        self.last_response = ""

    async def send(self, prompt: Any, **_kwargs: Any) -> None:
        return None

    async def receive_steps(self) -> Any:
        for step in self._steps:
            self.yielded += 1
            yield step

    async def cancel(self) -> None:
        self.cancels += 1
        if self._cancel_raises:
            raise RuntimeError("cancel boom")


def _antigravity_agent(conversation: _CountingConversation) -> AntigravityAgent:
    agent = AntigravityAgent(parse_agent_config(type=AgentKind.ANTIGRAVITY, model="gemini-3-flash"))  # type: ignore[arg-type]
    agent.working_directory = Path("/tmp")
    agent._sdk_agent = SimpleNamespace(conversation=conversation, is_started=True)
    return agent


async def _run_antigravity_communicate(
    *,
    n_steps: int = 3,
    stop_after: int | None = None,
    never: bool = False,
    cancel_raises: bool = False,
) -> tuple[AntigravityAgent, TurnRecord, _EventSink, _CountingConversation]:
    """Drive ``AntigravityAgent.communicate`` over a fake step stream (same
    stop_after / never / None semantics as the Claude and Codex drivers)."""
    conversation = _CountingConversation([_ag_step(i) for i in range(n_steps)], cancel_raises=cancel_raises)
    agent = _antigravity_agent(conversation)

    should_stop: Callable[[], bool] | None
    if stop_after is not None:
        should_stop = lambda: conversation.yielded >= stop_after  # noqa: E731
    elif never:
        should_stop = lambda: False  # noqa: E731
    else:
        should_stop = None

    sink = _EventSink()
    record = await agent.communicate("prompt", stream_callback=sink, should_stop=should_stop)
    return agent, record, sink, conversation


class TestAntigravityCooperativeStopSeam:
    async def test_stop_after_first_processed_step(self) -> None:
        agent, record, sink, conversation = await _run_antigravity_communicate(stop_after=1, n_steps=3)
        # The deciding step is kept; the next is never pulled.
        assert conversation.yielded == 1
        # The conversation was cancelled once (best-effort server-side cut).
        assert conversation.cancels == 1
        assert record.crashed is False
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.STOPPED_EARLY
        assert ends[0].crashed is False
        assert agent.pending_turn is None
        assert agent.get_state().value != "error"

    async def test_should_stop_none_consumes_full_stream(self) -> None:
        _agent, record, sink, conversation = await _run_antigravity_communicate(n_steps=3)
        assert conversation.yielded == 3
        assert conversation.cancels == 0
        assert record.crashed is False
        assert _agent_end_events(sink)[0].status == AgentEndStatus.COMPLETED

    async def test_should_stop_false_consumes_full_stream(self) -> None:
        _agent, _record, sink, conversation = await _run_antigravity_communicate(never=True, n_steps=3)
        assert conversation.yielded == 3
        assert _agent_end_events(sink)[0].status == AgentEndStatus.COMPLETED

    async def test_raising_cancel_still_stops_clean(self) -> None:
        # conversation.cancel() is best-effort: a raising cancel must not escalate
        # a stopped turn to a crash.
        agent, record, sink, conversation = await _run_antigravity_communicate(stop_after=1, cancel_raises=True)
        assert conversation.cancels == 1
        assert record.crashed is False
        assert _agent_end_events(sink)[0].status == AgentEndStatus.STOPPED_EARLY
        assert agent.pending_turn is None

    async def test_timeout_beats_stop_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FiringWatchdog:
            def __init__(self, *, on_timeout: Callable[[], None], **_kwargs: Any) -> None:
                self._on_timeout = on_timeout

            def __enter__(self) -> _FiringWatchdog:
                self._on_timeout()
                return self

            def __exit__(self, *_exc: Any) -> bool:
                return False

        monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _FiringWatchdog)
        conversation = _CountingConversation([_ag_step(0), _ag_step(1)])
        agent = _antigravity_agent(conversation)
        sink = _EventSink()
        with pytest.raises(TurnTimeoutError):
            await agent.communicate("prompt", stream_callback=sink, timeout=30.0, should_stop=lambda: True)
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.TIMEOUT
        assert ends[0].crashed is True
        assert agent.pending_turn is not None and agent.pending_turn.crashed is True
        assert AgentEndStatus.STOPPED_EARLY not in {e.status for e in ends}
        await agent.discard_pending_turn()

    async def test_post_stop_exception_stays_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The retry-poisoning gap, antigravity flavor: an exception raised by
        # post-stop cleanup (stand-in for the step generator's aclose() raising
        # after the break) lands in the generic handler, which must fall through
        # to the clean STOPPED_EARLY finalize instead of crash-finalizing.
        class _ExplodingExitWatchdog:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def __enter__(self) -> _ExplodingExitWatchdog:
                return self

            def __exit__(self, exc_type: Any, *_exc: Any) -> bool:
                if exc_type is None:
                    raise RuntimeError("post-stop cleanup boom")
                return False

        monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _ExplodingExitWatchdog)
        agent, record, sink, _conversation = await _run_antigravity_communicate(stop_after=1)
        assert record.crashed is False
        ends = _agent_end_events(sink)
        assert len(ends) == 1
        assert ends[0].status == AgentEndStatus.STOPPED_EARLY
        assert ends[0].crashed is False
        assert agent.pending_turn is None

    async def test_post_stop_cleanup_exception_without_stop_still_crashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Guard scoping: the same cleanup exception on a NON-stopped turn keeps
        # crashing (no behavior change for real failures).
        class _ExplodingExitWatchdog:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def __enter__(self) -> _ExplodingExitWatchdog:
                return self

            def __exit__(self, exc_type: Any, *_exc: Any) -> bool:
                if exc_type is None:
                    raise RuntimeError("cleanup boom")
                return False

        monkeypatch.setattr("coder_eval.agents.antigravity_agent.ThreadedWatchdog", _ExplodingExitWatchdog)
        conversation = _CountingConversation([_ag_step(0)])
        agent = _antigravity_agent(conversation)
        with pytest.raises(AgentCrashError):
            await agent.communicate("prompt", stream_callback=_EventSink(), should_stop=None)
        await agent.discard_pending_turn()


# --------------------------------------------------------------------------- #
# Orchestrator-level wiring on a non-Claude agent type: the watcher, gating and
# report row are agent-agnostic — an armed codex task flows end to end.
# --------------------------------------------------------------------------- #


class TestOrchestratorEarlyStopWiringCodex:
    _SKILL = "date-teller"

    def _criteria(self) -> list[Any]:
        return [
            _skill_crit(self._SKILL, self._SKILL, stop_when="pass"),
            FileExistsCriterion(path="artifact.txt", description="artifact must exist"),
        ]

    async def test_pass_stop_populates_early_stop_and_armed_gate(self, tmp_path) -> None:
        # A trailing event AFTER the deciding ToolEnd proves the cut: delivered == 3.
        events = [*_skill_events(self._SKILL), _turn_start()]
        result, agent = await _run_wiring(
            criteria=self._criteria(),
            events=events,
            scores=[1.0, 0.0],
            stop_early=True,
            tmp_path=tmp_path,
            agent_type=AgentKind.CODEX,
        )
        assert agent.delivered == 3
        assert result.early_stop is not None
        assert result.early_stop.reason == EarlyStopReason.CRITERION_PASSED
        # Armed-subset gate: advisory 0.0 is not gated on an early-stopped run.
        assert result.armed_criteria_passed(self._criteria()) is True
        # The report row carries the early-stop marker.
        row = eval_result_to_task_dict(result)
        assert row["stopped_early"] is True
