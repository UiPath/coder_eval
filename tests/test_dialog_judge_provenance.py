"""Real dialog persistence and detached grading keep the same agent evidence."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from coder_eval.agents.noop_agent import NoOpAgent
from coder_eval.criteria import agent_judge, llm_judge, system_one_judge
from coder_eval.errors import AgentCrashError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.evaluation.judge_context import JudgeContextBuilder
from coder_eval.models import NoneAgentConfig, TokenUsage
from coder_eval.orchestrator import Orchestrator
from coder_eval.simulation.user_simulator import UserSimulator
from coder_eval.streaming.callbacks import CompositeStreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import AgentEndEvent, AgentEndStatus, AgentStartEvent, TurnEndEvent, TurnStartEvent
from tests import test_grading_execution_completion as completion_fixtures
from tests.fixtures.text_stub_agent import TextStubAgent


cli = completion_fixtures.cli


def render(sandbox, records):
    context = JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=True,
        include_tool_calls=True,
        include_dialog=True,
        max_file_chars=10000,
    ).build(sandbox, None, records)
    return {
        "llm": llm_judge._render_user_message("Grade the agent answers.", context),
        "agent": agent_judge._render_user_message("Grade the agent answers.", context),
        "system_one": system_one_judge._build_state("Grade the agent answers.", context, 10000),
    }


class ProbeAgent(NoOpAgent):
    def __init__(self, mode):
        super().__init__(NoneAgentConfig(type="none"))
        self.mode, self.calls = mode, []

    async def start(self, working_directory, **kwargs):
        await super().start(working_directory, **kwargs)
        self.directory = Path(working_directory)

    async def communicate(self, user_input, *, stream_callback=None, **kwargs):
        self._begin_turn()
        self.calls.append(user_input)
        (self.directory / "retained.txt").write_text("Prior work remains unchanged.\n")
        crashed = self.mode == "crash" and self._iteration == 2
        empty = self.mode == "empty" and self._iteration == 2
        output = "PARTIAL_2" if crashed else "" if empty else f"FINAL_{self._iteration}"
        usage = TokenUsage() if empty else TokenUsage(uncached_input_tokens=2, output_tokens=1, total_cost_usd=0.125)
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])
        emit.on_event(AgentStartEvent(task_id="provenance", prompt=user_input, iteration=self._iteration))
        emit.on_event(TurnStartEvent(task_id="provenance", turn_id=str(len(self.calls))))
        emit.on_event(TurnEndEvent(task_id="provenance", turn_id=str(len(self.calls)), tokens=usage))
        emit.on_event(
            AgentEndEvent(
                task_id="provenance",
                iteration=self._iteration,
                user_input=user_input,
                agent_output=output,
                usage=usage,
                crashed=crashed,
                status=AgentEndStatus.CRASHED if crashed else AgentEndStatus.COMPLETED,
            )
        )
        record = collector.build_turn_record()
        if crashed:
            self.pending_turn = record
            raise AgentCrashError("Synthetic stream failure")
        self._end_turn_ok()
        return record


@pytest.fixture
def probe(cli, monkeypatch, tmp_path):
    state = {"mode": "stop", "agents": [], "grades": [], "failure_evidence": []}

    async def factory(self):
        agent = ProbeAgent(state["mode"])
        state["agents"].append(agent)
        return agent

    def simulator(*args, **kwargs):
        kwargs["agent_override"] = TextStubAgent(["QUESTION_2", "<END>"])
        return UserSimulator(*args, **kwargs)

    original_check = SuccessChecker.check_all_async

    async def capture(self, *args, **kwargs):
        state["grades"].append(render(self.sandbox, kwargs["turn_records"]))
        return await original_check(self, *args, **kwargs)

    original_communicate = Orchestrator._communicate_with_retry

    async def communication(self, **kwargs):
        try:
            return await original_communicate(self, **kwargs)
        except AgentCrashError:
            state["failure_evidence"].append(render(self.sandbox, self.result.iterations))
            raise

    monkeypatch.setattr(Orchestrator, "_create_agent", factory)
    monkeypatch.setattr(Orchestrator, "_communicate_with_retry", communication)
    monkeypatch.setattr("coder_eval.orchestrator.UserSimulator", simulator)
    monkeypatch.setattr(SuccessChecker, "check_all_async", capture)
    monkeypatch.setattr("coder_eval.errors.executor.get_retry_delay", lambda *_: 0)
    yield state
    (tmp_path / "judge-render-captures.json").write_text(
        json.dumps(
            {
                "grades": state["grades"],
                "failure_evidence": state["failure_evidence"],
                "agent_calls": [agent.calls for agent in state["agents"]],
            },
            indent=2,
        )
    )


def run_dialog(tmp_path, cli, probe, mode):
    probe["mode"] = mode
    definition = {
        "task_id": "provenance",
        "description": "Synthetic dialog provenance",
        "agent": {"type": "claude-code", "setting_sources": []},
        "sandbox": {"driver": "tempdir", "python": None, "node": None},
        "working_directory_prefix": False,
        "initial_prompt": "QUESTION_1",
        "run_limits": {"max_turn_retries": 1},
        "simulation": {
            "enabled": True,
            "persona": "Synthetic user",
            "goal": "Two answers",
            "max_turns": 5,
            "stop_token": "<END>",
            "stop_on_criteria_pass": False,
        },
        "success_criteria": [{"type": "file_exists", "path": "retained.txt", "description": "Prior file remains"}],
    }
    if mode == "scripted":
        definition.pop("initial_prompt")
        definition["simulation"] = {"enabled": True, "turns": ["QUESTION_1", "QUESTION_2"]}
    task = tmp_path / "task.yaml"
    task.write_text(yaml.safe_dump(definition))
    run = tmp_path / "run"
    result = cli(["run", str(task), "--run-dir", str(run)])
    assert result.exit_code == (1 if mode == "crash" else 0), result.output
    paths = list(run.rglob("task.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text())


def evaluate(cli, task_json, tmp_path):
    return cli(["evaluate", str(task_json.parent), "--run-dir", str(tmp_path / "grading")])


@pytest.mark.parametrize("mode", ["stop", "empty", "scripted"])
def test_real_run_saved_dialog_and_detached_grade_have_identical_judge_inputs(tmp_path, cli, probe, mode):
    path, before = run_dialog(tmp_path, cli, probe, mode)
    assert len(probe["grades"]) == 1
    initial = probe["grades"][0]
    assert [row["turn"] for row in initial["system_one"]["dialog"]] == [1, 2]
    assert initial["system_one"]["dialog"][1]["agent"] == ("" if mode == "empty" else "FINAL_2")
    artifacts = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.rglob("retained.txt")}
    assert len(artifacts) == 1
    calls = list(probe["agents"][0].calls)
    graded = evaluate(cli, path, tmp_path)
    assert graded.exit_code == 0, graded.output
    assert probe["grades"][-1] == initial
    after = json.loads(path.read_text())
    assert after["iterations"] == before["iterations"]
    assert artifacts == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.rglob("retained.txt")}
    assert probe["agents"][0].calls == calls and len(probe["agents"]) == 1
    assert len(after["iterations"]) == (2 if mode == "scripted" else 3)
    if mode != "scripted":
        assert after["iterations"][-1]["record_kind"] == "standalone_user"
        assert after["iterations"][1]["record_kind"] == "agent"


def test_all_failed_retries_keep_actual_agent_evidence_after_writer_and_evaluate(tmp_path, cli, probe):
    path, before = run_dialog(tmp_path, cli, probe, "crash")
    assert probe["agents"][0].calls == ["QUESTION_1", "QUESTION_2", "QUESTION_2"]
    assert len(probe["failure_evidence"]) == 1
    expected = probe["failure_evidence"][0]
    artifacts = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.rglob("retained.txt")}
    assert len(artifacts) == 1
    assert sum(r["token_usage"]["total_cost_usd"] for r in before["iterations"] if r["token_usage"]) == 0.375
    graded = evaluate(cli, path, tmp_path)
    assert graded.exit_code == 1, graded.output
    assert probe["grades"][-1] == expected
    after = json.loads(path.read_text())
    assert after["iterations"] == before["iterations"]
    assert [r["iteration"] for r in after["iterations"]] == [1, 2, 2, 2]
    assert [r["crashed"] for r in after["iterations"]] == [False, True, True, False]
    assert after["final_status"] == "ERROR" and len(probe["agents"]) == 1
    assert artifacts == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in path.parent.rglob("retained.txt")}
    assert [r["token_usage"] for r in after["iterations"]] == [r["token_usage"] for r in before["iterations"]]


def test_legacy_stop_flag_is_positive_provenance_without_inventing_a_kind(tmp_path, cli, probe):
    path, before = run_dialog(tmp_path, cli, probe, "stop")
    for row in before["iterations"]:
        row.pop("record_kind", None)
    path.write_text(json.dumps(before))
    initial = probe["grades"][0]
    graded = evaluate(cli, path, tmp_path)
    assert graded.exit_code == 0, graded.output
    assert probe["grades"][-1] == initial
    assert all(r.get("record_kind") is None for r in json.loads(path.read_text())["iterations"])


def test_legacy_failed_tail_stays_unknown_after_two_successful_detached_grades(tmp_path, cli, probe):
    path, before = run_dialog(tmp_path, cli, probe, "crash")
    for row in before["iterations"]:
        row.pop("record_kind", None)
    path.write_text(json.dumps(before))
    initial_incidents = len(before["error_history"])
    for _attempt in range(2):
        prior_grades = len(probe["grades"])
        graded = evaluate(cli, path, tmp_path)
        assert graded.exit_code == 1
        after = json.loads(path.read_text())
        assert all(r.get("record_kind") is None for r in after["iterations"])
        assert after["final_status"] == "ERROR"
        assert [r["agent_output"] for r in after["iterations"]] == [r["agent_output"] for r in before["iterations"]]
        assert len(probe["grades"]) == prior_grades + 1
        assert len(after["error_history"]) == initial_incidents, graded.output
        assert after["success_criteria_results"]
        assert all(row.get("error") is None for row in after["success_criteria_results"])
    assert len(probe["agents"]) == 1
