"""Tests for no-op / "agentless" tasks — "coder-eval without a coder" (issue #203).

The feature is modeled as a Null Object agent: a task selects ``agent: {type: none}``,
which binds to :class:`NoOpAgent`. ``agent.type`` is the single signal — there is no
separate ``agentless`` flag. Coverage spans:

  0. NoOpAgent unit: start/communicate/stop are no-ops; communicate returns an empty turn.
  1. Model validation (TaskDefinition.check_none_agent / check_prompt_fields): a no-op
     task needs no prompt and must not set initial_prompt / simulation, and every
     criterion must be agent-independent (no ``requires_agent``).
  2. Experiment resolution: ``type: none`` wins over a baseline coding agent injected by
     the default experiment (replace-scalar), and survives CLI / .env overrides.
  3. Loader: the prompt-required check is relaxed for a no-op task.
  4. Orchestrator end-to-end: NoOpAgent runs the normal lifecycle, makes no API call, and
     the success criteria are checked directly against the sandbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from coder_eval.agents import NoOpAgent
from coder_eval.config import settings
from coder_eval.models import (
    AgentKind,
    AgentState,
    ApiBackend,
    CommandExecutedCriterion,
    ExperimentDefaults,
    ExperimentDefinition,
    ExperimentVariant,
    FileContainsCriterion,
    FileExistsCriterion,
    FinalStatus,
    NoneAgentConfig,
    PreRunCommand,
    RunCommandCriterion,
    SandboxConfig,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.models.tasks import SimulationConfig
from coder_eval.orchestration.config import BatchRunConfig
from coder_eval.orchestration.experiment import _apply_cli_overrides, resolve_task_for_variant
from coder_eval.orchestration.task_loader import resolve_initial_prompt_file
from coder_eval.orchestrator import Orchestrator


def _none_task(criteria=None, **overrides) -> TaskDefinition:
    """A minimal valid no-op task (agent: {type: none}, no prompt)."""
    return TaskDefinition(
        task_id="noop",
        description="d",
        agent=parse_agent_config(type=AgentKind.NONE),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=criteria or [FileExistsCriterion(description="c", path="out.txt")],
        **overrides,
    )


# --------------------------------------------------------------------------- #
# Layer 0: NoOpAgent unit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestNoOpAgent:
    async def test_lifecycle_returns_empty_turn(self) -> None:
        """start/communicate/stop are no-ops; communicate returns an empty TurnRecord."""
        agent = NoOpAgent(NoneAgentConfig(type=AgentKind.NONE))
        await agent.start("/tmp/whatever")
        turn = await agent.communicate("this prompt is ignored")

        assert turn.agent_output == ""
        assert turn.iteration == 1
        assert turn.commands == []
        assert turn.token_usage is None

        await agent.stop()
        assert agent.get_state() == AgentState.FINISHED


# --------------------------------------------------------------------------- #
# Layer 1: model validation
# --------------------------------------------------------------------------- #
class TestNoneAgentValidation:
    def test_loads_without_prompt(self) -> None:
        """agent: {type: none} needs no initial_prompt and reports is_none_agent."""
        task = _none_task()
        assert task.is_none_agent is True
        assert task.agent is not None and task.agent.type == AgentKind.NONE
        assert task.initial_prompt is None

    def test_non_none_agent_still_requires_prompt(self) -> None:
        """The no-op exemption must not weaken the prompt requirement for real agents."""
        with pytest.raises(ValidationError, match="initial_prompt"):
            TaskDefinition(
                task_id="t",
                description="d",
                agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
                sandbox=SandboxConfig(driver="tempdir"),
                success_criteria=[FileExistsCriterion(description="c", path="o")],
            )

    def test_initial_prompt_rejected(self) -> None:
        """A no-op task must not set a prompt — no agent reads it."""
        with pytest.raises(ValidationError, match="must not set 'initial_prompt'"):
            _none_task(initial_prompt="do nothing")

    def test_simulation_rejected(self) -> None:
        """A no-op task cannot enable simulation — there is no agent to converse with."""
        with pytest.raises(ValidationError, match="cannot enable 'simulation'"):
            _none_task(simulation=SimulationConfig(enabled=True, persona="a user", goal="get help"))

    def test_requires_agent_criterion_rejected(self) -> None:
        """Criteria that inspect the agent trajectory cannot run when no agent runs."""
        with pytest.raises(ValidationError, match="require an agent trajectory"):
            _none_task(criteria=[CommandExecutedCriterion(description="agent ran curl")])

    def test_suggested_criteria_are_all_agent_independent(self) -> None:
        """The rejection message must not recommend a criterion that itself requires an agent.

        Guards against drift like the old text suggesting 'reference_comparison'
        (which is requires_agent=True). Cross-check every criterion type named in
        the suggestion list against the registry's requires_agent flag.
        """
        import re

        from coder_eval.models.criteria import BaseSuccessCriterion

        # type tag -> model class, so we can read each criterion's requires_agent flag.
        by_type = {
            cls.model_fields["type"].default: cls
            for cls in BaseSuccessCriterion.__subclasses__()
            if "type" in cls.model_fields
        }
        with pytest.raises(ValidationError) as exc:
            _none_task(criteria=[CommandExecutedCriterion(description="agent ran curl")])
        message = str(exc.value)
        # Pull the parenthesized suggestion list: "...Use agent-independent criteria (a, b, ...)."
        suggestion = re.search(r"agent-independent criteria \(([^)]*)\)", message)
        assert suggestion is not None, message
        suggested = {tok.strip() for tok in suggestion.group(1).split(",") if tok.strip() and tok.strip() != "..."}
        assert suggested, "expected at least one suggested criterion type"
        for ctype in suggested:
            assert ctype in by_type, f"message suggests unknown criterion type {ctype!r}"
            assert not by_type[ctype].requires_agent, f"message suggests agent-dependent criterion {ctype!r}"

    def test_allows_agent_independent_criteria(self) -> None:
        """run_command / file_exists / file_contains are all agent-independent."""
        task = _none_task(
            criteria=[
                RunCommandCriterion(description="cmd", command="true"),
                FileExistsCriterion(description="exists", path="o.txt"),
                FileContainsCriterion(description="contains", path="o.txt", includes=["x"]),
            ]
        )
        assert len(task.success_criteria) == 3


# --------------------------------------------------------------------------- #
# Layer 2: experiment resolution + CLI/.env overrides
# --------------------------------------------------------------------------- #
def _default_experiment_with_agent() -> ExperimentDefinition:
    """A default experiment that injects a baseline claude-code agent."""
    return ExperimentDefinition(
        experiment_id="default",
        defaults=ExperimentDefaults(agent={"type": "claude-code", "model": "claude-sonnet-4-6"}),
        variants=[ExperimentVariant(variant_id="v")],
    )


class TestNoneAgentResolution:
    def _resolve(self, task: TaskDefinition, config: BatchRunConfig | None = None) -> TaskDefinition:
        default_exp = _default_experiment_with_agent()
        experiment = ExperimentDefinition(experiment_id="exp", variants=[ExperimentVariant(variant_id="v")])
        resolved, _lineage, _repeats = resolve_task_for_variant(
            default_experiment=default_exp,
            task=task,
            experiment=experiment,
            variant=experiment.variants[0],
            config=config,
        )
        return resolved

    def test_none_type_wins_over_baseline_agent(self) -> None:
        """`type: none` is a replace-scalar, so it beats the default experiment's agent."""
        resolved = self._resolve(_none_task())
        assert resolved.is_none_agent is True
        assert resolved.agent is not None and resolved.agent.type == AgentKind.NONE

    def test_baseline_agent_injected_for_normal_task(self) -> None:
        """Control: a normal task still inherits the baseline agent (no regression)."""
        normal = TaskDefinition(
            task_id="normal",
            description="d",
            initial_prompt="do something",
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(description="c", path="o")],
        )
        resolved = self._resolve(normal)
        assert resolved.agent is not None
        assert resolved.agent.type == AgentKind.CLAUDE_CODE

    def test_cli_overrides_apply_to_none_task_without_breaking_contract(self) -> None:
        """A mixed-suite `-D agent.* -D run_limits.*` lands harmlessly on a no-op task.

        Covers the override path (orchestration.experiment._apply_cli_overrides): agent.*
        overrides settle on the NoneAgentConfig (NoOpAgent ignores them) and the task stays
        ``type: none``, while non-agent overrides (run_limits) still take effect.
        """
        resolved = self._resolve(_none_task())
        config = BatchRunConfig(
            run_dir=Path("."),
            overrides={"agent.model": "some-model", "run_limits.max_turns": 5},
        )
        _apply_cli_overrides(resolved, config)

        assert resolved.is_none_agent is True  # contract intact: still no-op
        assert resolved.agent is not None and resolved.agent.model == "some-model"
        assert resolved.run_limits is not None and resolved.run_limits.max_turns == 5

    def test_explicit_type_override_replaces_none(self) -> None:
        """`--type <x>` is highest-precedence and replaces `type: none` like for any task.

        Documents that suite-wide `--model` / `-D agent.*` is harmless (above), but an
        explicit `--type` is authoritative — it converts the no-op task to that agent.
        """
        resolved = self._resolve(_none_task())
        _apply_cli_overrides(resolved, BatchRunConfig(run_dir=Path("."), agent_type="codex"))
        assert resolved.agent is not None and resolved.agent.type == AgentKind.CODEX
        assert resolved.is_none_agent is False


# --------------------------------------------------------------------------- #
# Layer 3: loader prompt relaxation
# --------------------------------------------------------------------------- #
class TestNoneAgentLoader:
    def test_loader_does_not_require_prompt(self, tmp_path: Path) -> None:
        """resolve_initial_prompt_file accepts a no-op task with no prompt / prompt file."""
        task = _none_task()
        # Should not raise even though neither initial_prompt nor initial_prompt_file is set.
        out = resolve_initial_prompt_file(task, tmp_path)
        assert out.initial_prompt is None

    def test_loader_still_requires_prompt_for_real_agent(self, tmp_path: Path) -> None:
        """Control: a real-agent task with no prompt still trips the loader check."""
        task = TaskDefinition.model_construct(
            task_id="t",
            description="d",
            agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
            sandbox=SandboxConfig(driver="tempdir"),
            success_criteria=[FileExistsCriterion(description="c", path="o")],
            initial_prompt=None,
            initial_prompt_file=None,
            simulation=None,
        )
        with pytest.raises(ValueError, match="must be set"):
            resolve_initial_prompt_file(task, tmp_path)


# --------------------------------------------------------------------------- #
# Layer 4: orchestrator end-to-end (NoOpAgent, no network)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestNoneAgentRun:
    async def _run(self, task: TaskDefinition, tmp_path: Path, monkeypatch) -> Orchestrator:
        # Force the offline DIRECT route so _setup never reaches a proxy/bedrock path.
        monkeypatch.setattr(settings, "api_backend", ApiBackend.DIRECT)
        run_dir = tmp_path / "run" / task.task_id
        run_dir.mkdir(parents=True)
        orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
        await orch.run()
        return orch

    async def test_run_executes_criteria_via_noop_agent(self, tmp_path: Path, monkeypatch) -> None:
        """A deterministic no-op task passes its criteria, driven by NoOpAgent, no API call."""
        task = _none_task(
            criteria=[
                RunCommandCriterion(description="write proof", command="printf done > proof.txt"),
                FileExistsCriterion(description="exists", path="proof.txt"),
                FileContainsCriterion(description="content", path="proof.txt", includes=["done"]),
            ]
        )
        orch = await self._run(task, tmp_path, monkeypatch)

        assert isinstance(orch.agent, NoOpAgent)  # the Null Object agent ran
        assert orch.result is not None
        assert orch.result.final_status == FinalStatus.SUCCESS
        assert orch.result.agent_type == AgentKind.NONE
        assert orch.result.iteration_count == 1
        assert orch.result.agent_config is not None and orch.result.agent_config.type == AgentKind.NONE
        assert len(orch.result.success_criteria_results) == 3
        assert all(r.score >= 0.9 for r in orch.result.success_criteria_results)

    async def test_run_executes_pre_run(self, tmp_path: Path, monkeypatch) -> None:
        """pre_run still runs with the no-op agent — it's the only thing touching the sandbox.

        Like tasks/agentless_smoke_test.yaml, the deterministic side effect lives in pre_run
        and the criteria read it back. The command is a single bare token (no quotes, no
        spaces) so it survives both POSIX sh and the Windows cmd.exe shell.
        """
        token = "no-coder"
        task = _none_task(
            pre_run=[PreRunCommand(command=f"printf {token} > proof.txt")],
            criteria=[
                FileExistsCriterion(description="exists", path="proof.txt"),
                FileContainsCriterion(description="content", path="proof.txt", includes=[token]),
            ],
        )
        orch = await self._run(task, tmp_path, monkeypatch)

        assert isinstance(orch.agent, NoOpAgent)
        assert orch.result is not None
        assert orch.result.final_status == FinalStatus.SUCCESS
        assert len(orch.result.pre_run_results) == 1
        assert orch.result.pre_run_results[0].exit_code == 0

    async def test_run_reports_failure_on_failing_criterion(self, tmp_path: Path, monkeypatch) -> None:
        """A failing deterministic check lands the no-op task as FAILURE, not ERROR."""
        task = _none_task(criteria=[RunCommandCriterion(description="boom", command="exit 7")])
        orch = await self._run(task, tmp_path, monkeypatch)

        assert isinstance(orch.agent, NoOpAgent)
        assert orch.result is not None
        assert orch.result.final_status == FinalStatus.FAILURE
