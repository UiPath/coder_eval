"""Grading repair cannot complete skipped execution or erase its limits."""

import json
import socket
import subprocess
import sys

import pytest
import yaml
from typer.testing import CliRunner

from coder_eval.agents.noop_agent import NoOpAgent
from coder_eval.cli import app
from coder_eval.errors import ReferenceTamperedError
from coder_eval.errors.judge import JudgeInfrastructureError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import EvaluationResult, NoneAgentConfig, TokenUsage
from coder_eval.orchestrator import Orchestrator
from coder_eval.simulation.user_simulator import UserSimulator
from coder_eval.streaming.callbacks import CompositeStreamCallback
from coder_eval.streaming.collector import EventCollector
from coder_eval.streaming.events import AgentEndEvent, AgentStartEvent, TurnEndEvent, TurnStartEvent
from tests.fixtures.text_stub_agent import TextStubAgent


class KnownUsageAgent(NoOpAgent):
    async def communicate(self, user_input, *, stream_callback=None, **kwargs):
        self._begin_turn()
        collector = EventCollector()
        emit = CompositeStreamCallback([c for c in (collector, stream_callback) if c is not None])
        usage = TokenUsage(uncached_input_tokens=2, output_tokens=1, total_cost_usd=0.2)
        emit.on_event(AgentStartEvent(task_id="synthetic", prompt=user_input, iteration=self._iteration))
        emit.on_event(TurnStartEvent(task_id="synthetic", turn_id=str(self._iteration)))
        emit.on_event(TurnEndEvent(task_id="synthetic", turn_id=str(self._iteration), tokens=usage))
        emit.on_event(
            AgentEndEvent(
                task_id="synthetic",
                iteration=self._iteration,
                user_input=user_input,
                agent_output="Synthetic answer",
                usage=usage,
            )
        )
        self._end_turn_ok()
        return collector.build_turn_record()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Completion tests must not start processes or connect to a provider")

    connect = socket.socket.connect

    def no_external_connect(sock, address):
        # Only the stdlib socket pair may connect Windows asyncio's self-pipe.
        if (
            isinstance(address, tuple)
            and address[0] in {"127.0.0.1", "::1"}
            and sys._getframe(1).f_code is socket._fallback_socketpair.__code__
        ):
            return connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", no_external_connect)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("coder_eval.cli.run_helpers.settings.telemetry_enabled", False)
    monkeypatch.setattr("coder_eval.orchestrator.get_version_info", lambda *a, **k: {})
    monkeypatch.setattr("coder_eval.orchestration.batch.get_version_info", lambda *a, **k: {})
    monkeypatch.setattr("coder_eval.config.Settings.validate_api_keys", lambda *a, **k: None)
    monkeypatch.setattr("coder_eval.cli.run_helpers.settings.runs_dir", tmp_path / "default-runs")

    async def factory(self):
        return KnownUsageAgent(NoneAgentConfig(type="none"))

    def simulator(*args, **kwargs):
        kwargs["agent_override"] = TextStubAgent(["Second input", "Third input"])
        return UserSimulator(*args, **kwargs)

    monkeypatch.setattr(Orchestrator, "_create_agent", factory)
    monkeypatch.setattr("coder_eval.orchestrator.UserSimulator", simulator)
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text(
        yaml.safe_dump({"experiment_id": "synthetic", "defaults": {}, "variants": [{"variant_id": "fixture"}]}),
        encoding="utf-8",
    )
    runner = CliRunner()

    def invoke(args):
        if args[0] in {"run", "execute"}:
            args = [*args, "--experiment", str(experiment)]
        result = runner.invoke(app, args)
        assert result.exception is None or result.exit_code == 1, result.output
        return result

    return invoke


def task(tmp_path, *, dialog=None, budget=None):
    definition = {
        "task_id": "synthetic",
        "description": "Execution completion regression",
        "agent": {"type": "none"},
        "sandbox": {"driver": "tempdir", "python": None, "node": None},
        "success_criteria": [{"type": "file_exists", "path": ".", "description": "Sandbox exists"}],
    }
    if dialog:
        definition.update(
            agent={"type": "claude-code", "setting_sources": []},
            initial_prompt="First input",
            simulation={
                "enabled": True,
                "persona": "Synthetic user",
                "goal": "Three turns",
                "max_turns": 3,
                "check_criteria": dialog,
                "stop_on_criteria_pass": False,
            },
        )
    if budget:
        definition["run_limits"] = {budget: 0.1 if budget == "max_usd" else 1}
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(definition), encoding="utf-8")
    return path


def saved(run):
    paths = list(run.rglob("task.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def fail_one_grade(monkeypatch, error=None):
    original = SuccessChecker.check_all_async
    faults = []

    async def checker(self, *args, **kwargs):
        if not faults:
            faults.append(True)
            raise error or JudgeInfrastructureError("Synthetic grader failure")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SuccessChecker, "check_all_async", checker)
    return faults


def repair(cli, command, task_file, run, task_json):
    if command == "resume":
        return cli(["run", str(task_file), "--run-dir", str(run), "--resume"])
    return cli(["evaluate", str(task_json.parent), "--run-dir", str(run.parent / "grade")])


@pytest.mark.parametrize("command", ["resume", "evaluate"])
@pytest.mark.parametrize(
    "budget,status", [("max_total_tokens", "TOKEN_BUDGET_EXCEEDED"), ("max_usd", "COST_BUDGET_EXCEEDED")]
)
@pytest.mark.parametrize("dialog", [None, "every_turn", "end_of_dialog"])
def test_grader_fault_cannot_mask_configured_budget(tmp_path, monkeypatch, cli, command, budget, status, dialog):
    path = task(tmp_path, dialog=dialog, budget=budget)
    run = tmp_path / "run"
    fail_one_grade(monkeypatch)
    assert cli(["run", str(path), "--run-dir", str(run)]).exit_code == 1
    task_json, before = saved(run)
    assert before["final_status"] == status
    assert before["error_phase"] == "execution"
    assert before["error_history"][-1]["message"] == "Synthetic grader failure"
    assert before["weighted_score"] == 0.0
    repair(cli, command, path, run, task_json)
    after = json.loads(task_json.read_text(encoding="utf-8"))
    assert after["final_status"] == status
    assert after["error_phase"] == "execution"
    assert after["iterations"] == before["iterations"]


@pytest.mark.parametrize("command", ["resume", "evaluate"])
def test_interrupted_every_turn_dialog_stays_execution_error(tmp_path, monkeypatch, cli, command):
    path = task(tmp_path, dialog="every_turn")
    run = tmp_path / "run"
    fail_one_grade(monkeypatch)
    cli(["run", str(path), "--run-dir", str(run)])
    task_json, before = saved(run)
    assert before["iteration_count"] == 1
    assert before["simulation"]["stop_reason"] == "error"
    assert before["error_phase"] == "execution"
    repair(cli, command, path, run, task_json)
    after = json.loads(task_json.read_text(encoding="utf-8"))
    assert after["final_status"] == "ERROR"
    assert after["iterations"] == before["iterations"]


@pytest.mark.parametrize("dialog,turns", [(None, 1), ("end_of_dialog", 3)])
def test_completed_execution_still_repairs_without_replay(tmp_path, monkeypatch, cli, dialog, turns):
    path = task(tmp_path, dialog=dialog)
    run = tmp_path / "run"
    fail_one_grade(monkeypatch)
    cli(["run", str(path), "--run-dir", str(run)])
    task_json, before = saved(run)
    assert before["error_phase"] == "grading"
    assert before["iteration_count"] == turns
    repair(cli, "resume", path, run, task_json)
    after = json.loads(task_json.read_text(encoding="utf-8"))
    assert after["final_status"] == "SUCCESS"
    assert after["iterations"] == before["iterations"]
    assert after["weighted_score"] == 1.0


def test_reference_tampering_is_not_reclassified_as_grading_or_budget(tmp_path, monkeypatch, cli):
    path = task(tmp_path, budget="max_total_tokens")
    run = tmp_path / "run"
    fail_one_grade(monkeypatch, ReferenceTamperedError("Synthetic reference violation"))
    cli(["run", str(path), "--run-dir", str(run)])
    _, row = saved(run)
    assert row["final_status"] == "ERROR"
    assert row["error_phase"] == "execution"
    assert "grading_error" not in row


@pytest.mark.parametrize(
    "budget,status", [("max_total_tokens", "TOKEN_BUDGET_EXCEEDED"), ("max_usd", "COST_BUDGET_EXCEEDED")]
)
def test_final_scoring_fault_keeps_measured_criteria_and_budget_primary(tmp_path, monkeypatch, cli, budget, status):
    path = task(tmp_path, budget=budget)
    run = tmp_path / "run"
    original = EvaluationResult.calculate_weighted_score
    calls = []

    def calculate(self, criteria):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("Synthetic final scoring incident")
        return original(self, criteria)

    monkeypatch.setattr(EvaluationResult, "calculate_weighted_score", calculate)
    assert cli(["run", str(path), "--run-dir", str(run)]).exit_code == 1
    _, row = saved(run)
    assert len(calls) == 2
    assert row["final_status"] == status
    assert row["error_phase"] == "execution"
    assert "budget exceeded" in row["error_message"]
    assert row["error_history"][-1]["message"] == "Synthetic final scoring incident"
    assert len(row["success_criteria_results"]) == 1
    assert row["success_criteria_results"][0]["score"] == 1.0
    assert row["weighted_score"] is None


def test_cli_guard_allows_platform_socketpair_and_refuses_external_connections(cli):
    # Exercise the fallback Windows uses even on a host with native socketpair.
    reader, writer = socket._fallback_socketpair()
    try:
        writer.sendall(b"local")
        assert reader.recv(5) == b"local"
    finally:
        reader.close()
        writer.close()
    for address in (("127.0.0.1", 9), ("192.0.2.1", 443)):
        with socket.socket() as sock, pytest.raises(AssertionError, match="must not start processes"):
            sock.connect(address)


@pytest.mark.parametrize(
    "budget,status",
    [
        ("max_total_tokens", "TOKEN_BUDGET_EXCEEDED"),
        ("max_usd", "COST_BUDGET_EXCEEDED"),
    ],
)
@pytest.mark.parametrize("dialog", [None, "every_turn"])
def test_actual_budget_is_settled_before_checker(tmp_path, monkeypatch, cli, budget, status, dialog):
    instances = []
    entered = []
    original = Orchestrator.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(Orchestrator, "__init__", init)

    async def checker(self, *args, **kwargs):
        result = instances[-1].result
        entered.append((result.final_status.value, result.error_phase, result.error_message))
        raise JudgeInfrastructureError("Intentional checker outage after actual agent usage")

    monkeypatch.setattr(SuccessChecker, "check_all_async", checker)
    path = task(tmp_path, dialog=dialog, budget=budget)
    run = tmp_path / "run"
    outcome = cli(["run", str(path), "--run-dir", str(run)])
    assert outcome.exit_code == 1
    _, after = saved(run)
    assert len(entered) == 1
    assert entered[0][0:2] == (status, "execution")
    assert "budget exceeded" in entered[0][2]
    assert after["final_status"] == status and after["error_phase"] == "execution"
    assert after["error_message"] == entered[0][2]
    assert len(after["iterations"]) == 1 and after["iteration_count"] == 1
    assert any(
        item["message"] == "Intentional checker outage after actual agent usage" for item in after["error_history"]
    )
