"""The guards around detached grading, each tested on the branch that fires.

Every case here is a refusal, a skip, or a mode selection — the branches that
exist precisely because taking the other one would produce a plausible number
that is wrong. They were all shipped with coverage on the happy path only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from coder_eval.cli import app
from coder_eval.cli.evaluate_command import run_evaluation
from coder_eval.models import (
    AgentKind,
    EvaluationResult,
    FileExistsCriterion,
    FinalStatus,
    TaskDefinition,
    parse_agent_config,
)
from coder_eval.orchestrator import Orchestrator


runner = CliRunner()


# An agentless task: `type: none` runs no agent and forbids `initial_prompt`
# (there is nothing to read it), which is what makes it usable with no API key.
_AGENTLESS = """task_id: t
description: d
agent:
  type: none
success_criteria:
  - type: file_exists
    path: proof.txt
    description: x
"""

# A simulation task needs a real agent type — the refusal under `execute` fires
# during resolution, so the agent is never created.
_SIMULATED = """task_id: t
description: d
initial_prompt: p
agent:
  type: claude-code
simulation:
  enabled: true
  persona: a user
  goal: get it done
success_criteria:
  - type: file_exists
    path: proof.txt
    description: x
"""


def _task(tmp_path: Path, *, simulation: bool = False) -> Path:
    path = tmp_path / "t.yaml"
    path.write_text(_SIMULATED if simulation else _AGENTLESS, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# `execute` refuses simulation tasks
# --------------------------------------------------------------------------


def test_execute_refuses_a_simulation_task_by_name(tmp_path: Path) -> None:
    """The dialog loop reads criteria results to decide whether to keep talking,
    so an ungraded dialog would silently change its own stopping behavior. The
    refusal must name the task, or a user cannot tell which one to remove."""
    result = runner.invoke(app, ["execute", str(_task(tmp_path, simulation=True)), "--run-dir", str(tmp_path / "r")])

    assert result.exit_code != 0
    assert "simulation" in result.output.lower()
    assert "t" in result.output


def test_run_still_accepts_the_same_simulation_task(tmp_path: Path) -> None:
    """The control: the refusal is about `execute`, not about the task."""
    task = _task(tmp_path, simulation=True)
    with patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(return_value=(MagicMock(), 0))):
        result = runner.invoke(app, ["run", str(task), "--run-dir", str(tmp_path / "r")])
    assert "does not support simulation" not in result.output


# --------------------------------------------------------------------------
# The evaluate-only path refuses grade=False
# --------------------------------------------------------------------------


async def test_grading_off_on_the_evaluate_only_path_is_refused(tmp_path: Path) -> None:
    """No agent AND no grading is a no-op that would still write a task.json.
    Refusing beats producing an empty row that looks like a result."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v", grade=False)
    orch.success_checker = MagicMock()
    orch.result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
    )

    with pytest.raises(ValueError, match="meaningless on the evaluate-only path"):
        await orch._evaluation_loop()


# --------------------------------------------------------------------------
# --in-place / --copy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "expect_adopt"),
    [(None, False), ("--in-place", True), ("--copy", False)],
    ids=["default-for-a-work-dir-is-copy", "explicit-in-place", "explicit-copy"],
)
def test_the_flag_decides_adopt_versus_setup_on_a_work_dir(
    tmp_path: Path, flag: str | None, expect_adopt: bool
) -> None:
    """The choice is not cosmetic: the copy path filters node_modules / dist /
    build / .venv, so a criterion reading those fails as a copying artifact."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "proof.txt").write_text("x", encoding="utf-8")
    # --run-dir is not incidental: without it the grade lands in a repo-relative
    # runs/<timestamp>/, which several xdist workers race over.
    args = ["evaluate", str(_task(tmp_path)), str(work), "--run-dir", str(tmp_path / "r")]
    if flag:
        args.append(flag)

    with (
        patch("coder_eval.sandbox.Sandbox.adopt") as adopt,
        patch("coder_eval.sandbox.Sandbox.setup") as setup,
    ):
        runner.invoke(app, args)

    assert adopt.called is expect_adopt
    assert setup.called is not expect_adopt


def test_run_evaluation_has_real_defaults_not_typer_sentinels(tmp_path: Path) -> None:
    """`run_evaluation` exists because calling the Typer command in-process hands
    every unspecified option an `OptionInfo` — and `in_place=None` became truthy,
    silently flipping the copy default to in-place."""
    import inspect

    sig = inspect.signature(run_evaluation)
    for name in ("work_dir", "workspace", "in_place", "run_dir"):
        assert sig.parameters[name].default is None, f"{name} must default to a real None"
    assert sig.parameters["preserve"].default is True


# --------------------------------------------------------------------------
# The PATH round trip
# --------------------------------------------------------------------------


def test_the_agents_path_is_persisted_so_a_later_grade_can_restore_it(tmp_path: Path) -> None:
    """Without the persisted value a detached grade resolves `run_command`
    binaries against ambient PATH and can disagree with the run it grades."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v")
    orch.result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.FAILURE,
        iteration_count=0,
    )
    orch.sandbox = MagicMock()
    orch.agent = MagicMock()
    orch.agent.get_sdk_options.return_value = {"env": {"PATH": f"{tmp_path}:/usr/bin"}}

    orch._sync_sandbox_command_path_with_agent()

    assert "command_base_path" in orch.result.environment_info


def test_a_restored_path_drops_entries_inside_the_graded_workspace(tmp_path: Path) -> None:
    """The restored value is PREPENDED ahead of the host PATH and comes out of the
    run's own task.json. An entry inside the agent-writable workspace could
    shadow a real tool on the grader's host."""
    workspace = tmp_path / "ws"
    (workspace / "bin").mkdir(parents=True)
    outside = tmp_path / "toolchain"
    outside.mkdir()

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v")
    orch.sandbox = MagicMock()
    orch.sandbox.sandbox_dir = workspace

    kept = orch._sanitize_restored_path(f"{workspace / 'bin'}:{outside}:{tmp_path / 'gone'}")

    assert str(outside.resolve()) in kept
    assert str(workspace) not in kept, "an entry inside the graded tree must be dropped"
    assert "gone" not in kept, "a non-existent entry buys no parity"


# --------------------------------------------------------------------------
# The LiteLLM cost join
# --------------------------------------------------------------------------


def test_the_actual_cost_join_is_skipped_on_a_re_grade(tmp_path: Path) -> None:
    """The join keys on a per-Orchestrator nonce the prior turns never carried,
    so running it on a re-grade would clobber already-correct per-turn costs."""
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    prior = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 1, 1),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=0,
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v", prior_result=prior)
    orch.result = prior

    with patch("coder_eval.litellm_cost.apply_actual_cost") as apply:
        orch._join_litellm_actual_cost()

    apply.assert_not_called()
