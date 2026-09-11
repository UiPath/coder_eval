"""Round-trip tests: EvaluationResult -> ATIF Trajectory -> TurnRecords -> criteria.

Confirms a trajectory produced OUTSIDE this process (a Harbor agent's
``coder-eval execute --format harbor``) can still be graded by
``coder-eval evaluate --format harbor`` — the criteria that key off
``turn_records`` (``command_executed``, ``cli_called``, ``commands_efficiency``,
``skill_triggered``) must see the same commands after the ATIF round-trip.
"""

import asyncio
from datetime import UTC, datetime

from coder_eval.criteria.base import CheckContext
from coder_eval.criteria.command_executed import CommandExecutedChecker
from coder_eval.criteria.commands_efficiency import CommandsEfficiencyChecker
from coder_eval.criteria.skill_triggered import SkillTriggeredChecker
from coder_eval.harbor.atif_emit import evaluation_result_to_trajectory
from coder_eval.harbor.atif_hydrate import seed_from_atif_trajectory, trajectory_to_turn_records
from coder_eval.models import (
    AssistantMessage,
    CommandExecutedCriterion,
    CommandsEfficiencyCriterion,
    CommandTelemetry,
    ContentBlock,
    EvaluationResult,
    FinalStatus,
    SkillTriggeredCriterion,
    TurnRecord,
)


T0 = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _assistant(text: str = "working", **kwargs) -> AssistantMessage:
    return AssistantMessage(
        started_at=T0,
        completed_at=T0,
        generation_duration_ms=100.0,
        content_blocks=[ContentBlock(block_type="text", sequence=0, text=text)],
        **kwargs,
    )


def _cmd(
    tool_id: str, tool_name: str, *, index: int, parameters: dict | None = None, command: str = "ls"
) -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=T0,
        parameters=parameters if parameters is not None else {"command": command},
        result_status="success",
        result_summary=f"ran {command}",
        assistant_turn_index=index,
        sequence_number=index,
    )


def _original_result() -> EvaluationResult:
    turn = TurnRecord(
        iteration=1,
        user_input="do the task",
        agent_output="done",
        messages=[_assistant("running Bash"), _assistant("running Skill")],
        commands=[
            _cmd("toolu_a", "Bash", index=0, command="uv run pytest"),
            _cmd("toolu_b", "Skill", index=1, parameters={"skill": "my-skill"}),
        ],
    )
    return EvaluationResult(
        task_id="atif_hydrate_test",
        task_description="round-trip test",
        variant_id="default",
        agent_type="claude-code",
        model_used="claude-sonnet-5",
        started_at=T0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        iterations=[turn],
    )


class TestRoundTrip:
    def test_commands_survive_emit_then_hydrate(self):
        original = _original_result()
        trajectory = evaluation_result_to_trajectory(original)
        assert trajectory is not None

        turns = trajectory_to_turn_records(trajectory)
        all_commands = [cmd for turn in turns for cmd in turn.commands]
        assert {cmd.tool_id for cmd in all_commands} == {"toolu_a", "toolu_b"}
        assert {cmd.tool_name for cmd in all_commands} == {"Bash", "Skill"}

    def test_seed_from_atif_trajectory_builds_valid_result(self):
        trajectory = evaluation_result_to_trajectory(_original_result())
        seeded = seed_from_atif_trajectory(trajectory, task_id="atif_hydrate_test")
        assert seeded.iteration_count == len(seeded.iterations)
        assert seeded.agent_type == "claude-code"


class TestCriteriaAgainstHydratedTrajectory:
    def _turn_records(self) -> list[TurnRecord]:
        trajectory = evaluation_result_to_trajectory(_original_result())
        assert trajectory is not None
        return trajectory_to_turn_records(trajectory)

    def test_command_executed_matches_hydrated_bash_call(self):
        turn_records = self._turn_records()
        checker = CommandExecutedChecker()
        criterion = CommandExecutedCriterion(
            description="ran pytest",
            tool_name="Bash",
            command_pattern="pytest",
            min_count=1,
        )
        result = asyncio.run(
            checker.check_async(criterion, sandbox=None, turn_records=turn_records, context=CheckContext())
        )
        assert result.score == 1.0

    def test_commands_efficiency_counts_hydrated_commands(self):
        turn_records = self._turn_records()
        checker = CommandsEfficiencyChecker()
        criterion = CommandsEfficiencyCriterion(description="efficiency", expected_commands=2)
        result = asyncio.run(
            checker.check_async(criterion, sandbox=None, turn_records=turn_records, context=CheckContext())
        )
        assert result.score == 1.0

    def test_skill_triggered_sees_hydrated_skill_call(self):
        turn_records = self._turn_records()
        checker = SkillTriggeredChecker()
        criterion = SkillTriggeredCriterion(description="skill", skill_name="my-skill", expected_skill="my-skill")
        result = asyncio.run(
            checker.check_async(criterion, sandbox=None, turn_records=turn_records, context=CheckContext())
        )
        assert result.score == 1.0
