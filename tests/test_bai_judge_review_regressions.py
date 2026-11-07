"""Concrete public judge regressions from the c64 review."""

import json

import pytest

from coder_eval.agents.noop_agent import NoOpAgent
from coder_eval.criteria import agent_judge, llm_judge, system_one_judge
from coder_eval.errors import AgentCrashError
from coder_eval.evaluation.judge_context import JudgeContextBuilder
from coder_eval.models import NoneAgentConfig, SandboxConfig, TokenUsage
from coder_eval.orchestrator import Orchestrator
from coder_eval.sandbox import Sandbox
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent
from tests import test_dialog_judge_provenance as flow
from tests import test_judge_retry_attempt_labels as retry_fixtures


cli = flow.cli
probe = flow.probe
offline_judge = retry_fixtures.offline_judge


def record(number, output, *, crashed=False):
    collector = EventCollector()
    collector.on_event(AgentStartEvent(task_id="review", iteration=number, prompt=f"QUESTION_{number}"))
    collector.on_event(
        AgentEndEvent(
            task_id="review",
            iteration=number,
            user_input=f"QUESTION_{number}",
            agent_output=output,
            crashed=crashed,
            status=AgentEndStatus.CRASHED if crashed else AgentEndStatus.COMPLETED,
            usage=TokenUsage(output_tokens=1),
        )
    )
    return collector.build_turn_record()


def context(records, *, dialog=False):
    return JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=True,
        include_tool_calls=True,
        include_dialog=dialog,
        max_file_chars=10000,
    ).build(Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "review"), None, records)


def test_recovered_output_without_dialog_has_healthy_bytes_for_all_renderers():
    healthy = context([record(2, "FINAL_2")])
    recovered = context([record(2, "PARTIAL_2", crashed=True), record(2, "FINAL_2")])
    for renderer in (llm_judge._render_user_message, agent_judge._render_user_message):
        assert renderer("Grade agent_output", recovered) == renderer("Grade agent_output", healthy)
    assert system_one_judge._build_state("Grade agent_output", recovered, 10000) == system_one_judge._build_state(
        "Grade agent_output", healthy, 10000
    )


def test_failed_output_keeps_system_one_agent_output_key():
    state = system_one_judge._build_state("Grade agent_output", context([record(2, "PARTIAL_2", crashed=True)]), 10000)
    assert state.get("agent_output") == "PARTIAL_2"
    assert "failed_agent_output" not in state
    assert state.get("agent_attempt", {}).get("crashed") is True


def test_real_legacy_crashed_dialog_can_be_graded_without_rewriting_rows(tmp_path, cli, probe, offline_judge):
    path, before = flow.run_dialog(tmp_path, cli, probe, "crash")
    for row in before["iterations"]:
        row.pop("record_kind", None)
    before["task_config"]["resolved"]["success_criteria"] = [
        {
            "type": "llm_judge",
            "description": "Legacy dialog grade",
            "prompt": "Grade the logical turn2 answer.",
            "include_dialog": True,
            "include_reference": False,
            "include_agent_output": False,
            "model": "openai/synthetic-offline",
            "capture_transcript": True,
        }
    ]
    before["task_config"]["resolved"]["checker_context"] = {
        "api_route": {"route": "litellm", "model": "openai/synthetic-offline"}
    }
    path.write_text(json.dumps(before))
    prior_grades = len(probe["grades"])
    graded = cli(["evaluate", str(path.parent), "--run-dir", str(tmp_path / "grading"), "--allow-recorded-commands"])
    assert graded.exit_code == 1
    assert len(probe["grades"]) == prior_grades + 1
    after = json.loads(path.read_text())
    assert after["final_status"] == "ERROR"
    assert after["success_criteria_results"]
    assert len(offline_judge) == 1
    assert "omitted legacy user-only duplicate" in after["success_criteria_results"][0]["details"]
    assert all(row.get("record_kind") is None for row in after["iterations"])
    assert all("ambiguous legacy" not in str(row) for row in after["success_criteria_results"])
    normalized = [{key: value for key, value in row.items() if key != "record_kind"} for row in after["iterations"]]
    assert normalized == before["iterations"]


class NoPartialAgent(flow.ProbeAgent):
    async def communicate(self, user_input, *, stream_callback=None, **kwargs):
        if self._iteration >= 1:
            self._begin_turn()
            self.calls.append(user_input)
            self.pending_turn = None
            raise AgentCrashError("Synthetic failure without a pending partial")
        return await super().communicate(user_input, stream_callback=stream_callback, **kwargs)


def test_no_partial_crash_question_survives_real_writer_and_detached_grade(tmp_path, cli, probe, monkeypatch):
    async def factory(self):
        agent = NoPartialAgent("crash")
        probe["agents"].append(agent)
        return agent

    monkeypatch.setattr(Orchestrator, "_create_agent", factory)
    path, before = flow.run_dialog(tmp_path, cli, probe, "crash")
    assert probe["agents"][0].calls == ["QUESTION_1", "QUESTION_2", "QUESTION_2"]
    assert [row["record_kind"] for row in before["iterations"]] == ["agent", "standalone_user"]
    flow.evaluate(cli, path, tmp_path)
    assert probe["grades"]
    for renderer, value in probe["grades"][-1].items():
        assert "QUESTION_2" in json.dumps(value), renderer
    assert json.loads(path.read_text())["iterations"] == before["iterations"]


@pytest.mark.asyncio
async def test_real_noop_empty_completed_legacy_record_remains_visible(tmp_path):
    agent = NoOpAgent(NoneAgentConfig(type="none"))
    await agent.start(str(tmp_path))
    empty = await agent.communicate("QUESTION_2")
    empty = empty.model_copy(update={"iteration": 2, "record_kind": None})
    built = context([record(2, "PARTIAL_2", crashed=True), empty], dialog=True)
    assert built.dialog[-1] == ("QUESTION_2", "")
    await agent.stop()
