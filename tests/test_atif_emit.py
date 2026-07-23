"""Tests for the EvaluationResult → ATIF Trajectory converter (atif_emit)."""

import json
from datetime import UTC, datetime

from coder_eval.harbor import Trajectory, evaluation_result_to_trajectory, write_trajectory_json
from coder_eval.models import (
    AssistantMessage,
    CommandTelemetry,
    ContentBlock,
    EvaluationResult,
    FinalStatus,
    ReconciliationMessage,
    TokenUsage,
    TurnRecord,
    UserMessage,
)
from coder_eval.path_utils import atomic_write_text


T0 = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


def _assistant(
    text: str = "working on it",
    *,
    thinking: str | None = None,
    parent_tool_use_id: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 20,
    cache_read: int = 30,
    model: str = "claude-sonnet-4-6",
) -> AssistantMessage:
    blocks = [ContentBlock(block_type="text", sequence=0, text=text)]
    if thinking is not None:
        blocks.insert(0, ContentBlock(block_type="thinking", sequence=0, thinking=thinking))
    return AssistantMessage(
        started_at=T0,
        completed_at=T0,
        generation_duration_ms=1000.0,
        content_blocks=blocks,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        model=model,
        parent_tool_use_id=parent_tool_use_id,
    )


def _cmd(tool_id: str, *, index: int | None, tool_name: str = "Bash", command: str = "ls") -> CommandTelemetry:
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=T0,
        parameters={"command": command},
        result_status="success",
        result_summary=f"output of {tool_id}",
        assistant_turn_index=index,
        sequence_number=0,
    )


def _turn(
    *,
    iteration: int = 1,
    user_input: str = "do the task",
    agent_output: str = "done",
    messages: list | None = None,
    commands: list[CommandTelemetry] | None = None,
    token_usage: TokenUsage | None = None,
    crashed: bool = False,
    crash_reason: str | None = None,
) -> TurnRecord:
    return TurnRecord(
        iteration=iteration,
        user_input=user_input,
        agent_output=agent_output,
        messages=messages or [],
        commands=commands or [],
        token_usage=token_usage,
        crashed=crashed,
        crash_reason=crash_reason,
    )


def _result(turns: list[TurnRecord], *, total_token_usage: TokenUsage | None = None) -> EvaluationResult:
    return EvaluationResult(
        task_id="atif_emit_test",
        task_description="converter test",
        variant_id="default",
        agent_type="claude-code",
        model_used="claude-sonnet-4-6",
        started_at=T0,
        final_status=FinalStatus.SUCCESS,
        iteration_count=len(turns),
        iterations=turns,
        total_token_usage=total_token_usage,
    )


class TestHappyPath:
    def test_user_step_then_agent_steps_with_tool_attribution(self):
        messages = [_assistant("first gen"), _assistant("second gen")]
        commands = [_cmd("toolu_a", index=0), _cmd("toolu_b", index=1), _cmd("toolu_c", index=1)]
        result = _result([_turn(messages=messages, commands=commands)])

        t = evaluation_result_to_trajectory(result)
        assert t is not None
        # Synthetic user step (no UserMessage in stream) + 2 agent steps.
        assert [s.source for s in t.steps] == ["user", "agent", "agent"]
        assert [s.step_id for s in t.steps] == [1, 2, 3]
        assert t.steps[0].message == "do the task"
        # Commands attached to their own generation.
        assert [tc.tool_call_id for tc in t.steps[1].tool_calls] == ["toolu_a"]
        assert [tc.tool_call_id for tc in t.steps[2].tool_calls] == ["toolu_b", "toolu_c"]
        # Observation joins by source_call_id within the same step (validator-checked).
        assert t.steps[2].observation.results[0].source_call_id == "toolu_b"
        assert t.steps[2].observation.results[0].content == "output of toolu_b"
        assert t.session_id == "atif_emit_test/default"
        assert t.agent.name == "claude-code"
        assert t.agent.version  # coder_eval __version__, non-empty

    def test_metrics_mapping_uses_token_usage_derivation(self):
        messages = [_assistant(input_tokens=100, output_tokens=50, cache_creation=20, cache_read=30)]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages)]))
        m = t.steps[1].metrics
        # prompt = uncached(100) + cache_creation(20) + cache_read(30) — via TokenUsage.input_tokens.
        assert m.prompt_tokens == 150
        assert m.completion_tokens == 50
        assert m.cached_tokens == 30
        # Finer split preserved in extra.
        assert t.steps[1].extra["cache_creation_tokens"] == 20

    def test_reasoning_content_from_thinking_blocks(self):
        messages = [_assistant("answer", thinking="let me think")]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages)]))
        assert t.steps[1].reasoning_content == "let me think"
        assert t.steps[1].message == "answer"

    def test_explicit_user_message_becomes_user_step(self):
        messages = [UserMessage(text="simulated utterance", completed_at=T0), _assistant("reply")]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages)]))
        # Explicit UserMessage → no synthetic user step is added.
        assert [s.source for s in t.steps] == ["user", "agent"]
        assert t.steps[0].message == "simulated utterance"
        assert t.steps[0].timestamp == T0.isoformat()


class TestSubagentNesting:
    def test_subagent_generations_nest_not_flatten(self):
        spawn = _cmd("toolu_task", index=0, tool_name="Agent", command="")
        messages = [
            _assistant("spawning a sub-agent"),
            _assistant("sub work 1", parent_tool_use_id="toolu_task"),
            _assistant("sub work 2", parent_tool_use_id="toolu_task"),
            _assistant("main continues"),
        ]
        sub_cmd = _cmd("toolu_sub", index=1)  # index 1 = first sub-agent generation
        result = _result([_turn(messages=messages, commands=[spawn, sub_cmd])])

        t = evaluation_result_to_trajectory(result)
        # Main thread: user + 2 main agent steps only.
        assert [s.source for s in t.steps] == ["user", "agent", "agent"]
        assert t.steps[1].message == "spawning a sub-agent"
        assert t.steps[2].message == "main continues"
        # One embedded child, re-indexed from 1, carrying the sub-agent's command.
        assert len(t.subagent_trajectories) == 1
        child = t.subagent_trajectories[0]
        assert child.trajectory_id == "toolu_task"
        assert [s.step_id for s in child.steps] == [1, 2]
        assert child.steps[0].tool_calls[0].tool_call_id == "toolu_sub"
        assert child.agent.version == t.agent.version
        # The spawning call's observation references the child.
        spawn_result = next(r for r in t.steps[1].observation.results if r.source_call_id == "toolu_task")
        assert spawn_result.subagent_trajectory_ref[0].trajectory_id == "toolu_task"

    def test_orphan_subagent_group_embeds_without_ref(self):
        # Sub-agent messages whose spawning tool call is not in the telemetry.
        messages = [_assistant("main"), _assistant("orphan sub", parent_tool_use_id="toolu_ghost")]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages)]))
        assert t.subagent_trajectories[0].trajectory_id == "toolu_ghost"
        # No fabricated tool call / ref on the main thread.
        for step in t.steps:
            assert not step.tool_calls or all(tc.tool_call_id != "toolu_ghost" for tc in step.tool_calls)


class TestGenerationlessTurn:
    def test_user_message_and_reconciliation_survive_without_generations(self):
        """A turn whose stream has user/reconciliation entries but NO assistant
        generations (e.g. a simulation turn that crashed before the first
        generation) must not fall into the legacy path — the UserMessage still
        becomes a user step and the residual is still recorded."""
        messages = [
            UserMessage(text="simulated ask", completed_at=T0),
            ReconciliationMessage(input_tokens=42, note="billed, never streamed"),
        ]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages, agent_output="")]))
        assert [s.source for s in t.steps] == ["user"]
        assert t.steps[0].message == "simulated ask"
        assert t.extra["reconciliation"][0]["input_tokens"] == 42


class TestReconciliation:
    def test_reconciliation_never_a_step_and_totals_match(self):
        total = TokenUsage(
            uncached_input_tokens=612,
            cache_creation_input_tokens=20,
            cache_read_input_tokens=430,
            output_tokens=50,
            total_cost_usd=0.05,
        )
        messages = [
            _assistant(input_tokens=100, output_tokens=50, cache_creation=20, cache_read=30),
            ReconciliationMessage(input_tokens=512, cache_read_tokens=400, note="prompt slice"),
        ]
        result = _result(
            [_turn(messages=messages, token_usage=total)],
            total_token_usage=total,
        )
        t = evaluation_result_to_trajectory(result)
        assert all(s.source != "reconciliation" for s in t.steps)  # not representable anyway
        assert len(t.steps) == 2  # user + one generation
        # Residual recorded at root.
        recon = t.extra["reconciliation"]
        assert recon == [
            {
                "iteration": 1,
                "input_tokens": 512,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 400,
                "note": "prompt slice",
            }
        ]
        # FinalMetrics ≡ authoritative total (which already includes the residual).
        fm = t.final_metrics
        assert fm.total_prompt_tokens == total.input_tokens == 1062
        assert fm.total_completion_tokens == 50
        assert fm.total_cached_tokens == 430
        assert fm.total_cost_usd == 0.05
        assert fm.total_steps == 2

    def test_final_metrics_falls_back_to_summing_turns(self):
        u1 = TokenUsage(uncached_input_tokens=10, output_tokens=5)
        u2 = TokenUsage(uncached_input_tokens=20, output_tokens=15)
        result = _result(
            [
                _turn(iteration=1, messages=[_assistant()], token_usage=u1),
                _turn(iteration=2, messages=[_assistant()], token_usage=u2),
            ],
        )
        t = evaluation_result_to_trajectory(result)
        assert t.final_metrics.total_prompt_tokens == 30
        assert t.final_metrics.total_completion_tokens == 20

    def test_no_usage_anywhere_omits_final_metrics(self):
        t = evaluation_result_to_trajectory(_result([_turn(messages=[_assistant()])]))
        # per-message metrics exist but no turn/total usage → final_metrics omitted.
        assert t.final_metrics is None


class TestLegacyAndEdgeCases:
    def test_legacy_turn_without_messages(self):
        usage = TokenUsage(uncached_input_tokens=100, cache_read_input_tokens=40, output_tokens=25)
        commands = [_cmd("toolu_x", index=None)]
        result = _result([_turn(messages=[], commands=commands, token_usage=usage)])
        t = evaluation_result_to_trajectory(result)
        assert [s.source for s in t.steps] == ["user", "agent"]
        agent_step = t.steps[1]
        assert agent_step.message == "done"
        assert agent_step.metrics.prompt_tokens == 140
        assert agent_step.tool_calls[0].tool_call_id == "toolu_x"

    def test_unattributed_command_rides_last_main_agent_step(self):
        messages = [_assistant("gen 1"), _assistant("gen 2")]
        commands = [_cmd("toolu_none", index=None), _cmd("toolu_oob", index=99)]
        t = evaluation_result_to_trajectory(_result([_turn(messages=messages, commands=commands)]))
        last_agent = t.steps[-1]
        ids = [tc.tool_call_id for tc in last_agent.tool_calls]
        assert ids == ["toolu_none", "toolu_oob"]

    def test_leftover_commands_stay_within_their_turn(self):
        """A turn with a stream but no main-thread agent step must synthesize its
        own agent step for leftover commands — never attach them to a previous
        iteration's step (which would mislabel the command's iteration)."""
        turn1 = _turn(iteration=1, messages=[_assistant("gen 1")])
        turn2 = _turn(
            iteration=2,
            user_input="follow-up",
            agent_output="crashed early",
            messages=[UserMessage(text="follow-up", completed_at=T0)],
            commands=[_cmd("toolu_orphan", index=None)],
        )
        t = evaluation_result_to_trajectory(_result([turn1, turn2]))
        # Turn 1's agent step must NOT have absorbed turn 2's command.
        turn1_agent = next(s for s in t.steps if s.source == "agent" and s.extra["iteration"] == 1)
        assert not turn1_agent.tool_calls
        # Turn 2 synthesized its own agent step carrying the command.
        turn2_agent = next(s for s in t.steps if s.source == "agent" and s.extra["iteration"] == 2)
        assert [tc.tool_call_id for tc in turn2_agent.tool_calls] == ["toolu_orphan"]
        assert turn2_agent.message == "crashed early"

    def test_multi_iteration_sequential_step_ids_and_user_steps(self):
        result = _result(
            [
                _turn(iteration=1, user_input="first ask", messages=[_assistant("a")]),
                _turn(iteration=2, user_input="feedback", messages=[_assistant("b")]),
            ]
        )
        t = evaluation_result_to_trajectory(result)
        assert [s.step_id for s in t.steps] == [1, 2, 3, 4]
        assert [s.source for s in t.steps] == ["user", "agent", "user", "agent"]
        assert t.steps[2].message == "feedback"
        assert t.steps[2].extra["iteration"] == 2

    def test_crashed_turn_marks_steps(self):
        result = _result([_turn(messages=[_assistant("partial work")], crashed=True, crash_reason="timeout")])
        t = evaluation_result_to_trajectory(result)
        assert t.steps[0].extra["crashed"] is True
        assert t.steps[0].extra["crash_reason"] == "timeout"  # first step of the turn only
        assert t.steps[1].extra["crashed"] is True
        assert "crash_reason" not in t.steps[1].extra

    def test_empty_result_returns_none(self):
        assert evaluation_result_to_trajectory(_result([])) is None

    def test_converter_does_not_mutate_input(self):
        result = _result([_turn(messages=[_assistant()], commands=[_cmd("toolu_a", index=0)])])
        before = result.model_dump_json()
        evaluation_result_to_trajectory(result)
        assert result.model_dump_json() == before


class TestWriteTrajectoryJson:
    def test_writes_valid_atif_json(self, tmp_path):
        result = _result([_turn(messages=[_assistant()])])
        path = tmp_path / "trajectory.json"
        written = write_trajectory_json(result, path)
        assert written == path
        parsed = Trajectory.model_validate(json.loads(path.read_text(encoding="utf-8")))
        assert parsed.session_id == "atif_emit_test/default"
        assert not path.with_suffix(".json.tmp").exists()

    def test_zero_step_result_writes_nothing(self, tmp_path):
        path = tmp_path / "trajectory.json"
        assert write_trajectory_json(_result([]), path) is None
        assert not path.exists()

    def test_converter_exception_swallowed(self, tmp_path, monkeypatch):
        import coder_eval.harbor.atif_emit as emit

        monkeypatch.setattr(emit, "evaluation_result_to_trajectory", lambda _: 1 / 0)
        path = tmp_path / "trajectory.json"
        assert emit.write_trajectory_json(_result([_turn()]), path) is None
        assert not path.exists()


class TestAtomicWriteText:
    def test_writes_and_leaves_no_tmp(self, tmp_path):
        path = tmp_path / "out.json"
        atomic_write_text(path, '{"a": 1}')
        assert path.read_text(encoding="utf-8") == '{"a": 1}'
        assert list(tmp_path.iterdir()) == [path]
