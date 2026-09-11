"""The guards around detached grading, each tested on the branch that fires.

Every case here is a refusal, a skip, or a mode selection — the branches that
exist precisely because taking the other one would produce a plausible number
that is wrong. They were all shipped with coverage on the happy path only.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import click
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
# `run` refuses a task with zero success_criteria; `execute` still accepts it
# --------------------------------------------------------------------------

_NO_CRITERIA = """task_id: t
description: d
agent:
  type: none
success_criteria: []
"""


def test_run_refuses_a_task_with_no_success_criteria(tmp_path: Path) -> None:
    """An empty `success_criteria:` scores vacuously (all_criteria_passed([])
    is True, calculate_weighted_score([]) is 0.0), so a graded run of such a
    task would silently finalize as SUCCESS at weighted_score 0.0 -- a
    misconfigured task, not a real result. `run` must refuse it by name."""
    path = tmp_path / "t.yaml"
    path.write_text(_NO_CRITERIA, encoding="utf-8")

    result = runner.invoke(app, ["run", str(path), "--run-dir", str(tmp_path / "r")])

    assert result.exit_code != 0
    assert "success_criteria" in result.output
    assert "t" in result.output


def test_execute_still_accepts_a_task_with_no_success_criteria(tmp_path: Path) -> None:
    """The control: `execute` never grades, so a criteria-free task.yaml (the
    shape the Harbor agent-phase export deliberately produces) is legal there."""
    path = tmp_path / "t.yaml"
    path.write_text(_NO_CRITERIA, encoding="utf-8")

    with patch("coder_eval.cli.run_command._run_with_experiment", new=AsyncMock(return_value=(MagicMock(), 0))):
        result = runner.invoke(app, ["execute", str(path), "--run-dir", str(tmp_path / "r")])

    assert "success_criteria" not in result.output


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


def test_a_restored_path_drops_entries_inside_the_graded_run(tmp_path: Path) -> None:
    """The restored value is PREPENDED ahead of the host PATH and comes out of the
    run's own task.json — a shareable artifact. Every entry an attacker could
    have placed there must be dropped; only the run's real toolchain survives.

    The run-directory SIBLING case is the one this test used to pin the wrong way
    round: it asserted such an entry was kept. The workspace is only part of the
    run dir, and ``artifacts/`` and the run root travel in the same archive.
    """
    run_dir = tmp_path / "run"
    workspace = run_dir / "ws"
    (workspace / "bin").mkdir(parents=True)
    sibling = run_dir / "artifacts-shim"  # inside the run dir, outside the workspace
    sibling.mkdir()
    toolchain = tmp_path / "toolchain"  # a genuine location outside the run entirely
    toolchain.mkdir()
    relative = Path("evilbin")

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        agent=parse_agent_config(type=AgentKind.CLAUDE_CODE),
        success_criteria=[FileExistsCriterion(path="x.txt", description="x")],
    )
    orch = Orchestrator(task=task, run_dir=run_dir, variant_id="v")
    orch.sandbox = MagicMock()
    orch.sandbox.sandbox_dir = workspace

    # os.pathsep, not a hardcoded ":" — the separator is ";" on Windows, where a
    # colon-joined value parses as one (non-existent) entry and every assertion
    # below passes vacuously against an empty result.
    recorded = os.pathsep.join(
        [
            str(workspace / "bin"),
            str(sibling),
            str(relative),
            str(toolchain),
            str(tmp_path / "gone"),
        ]
    )
    kept = orch._sanitize_restored_path(recorded)

    assert str(toolchain.resolve()) in kept, "a real out-of-run toolchain entry is the point of the restore"
    assert str(workspace) not in kept, "an entry inside the graded workspace must be dropped"
    assert str(sibling) not in kept, "an entry elsewhere in the run directory must be dropped too"
    assert "evilbin" not in kept, "a relative entry would resolve against the grader's cwd"
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


@pytest.mark.asyncio
async def test_a_container_run_records_the_driver_it_was_authored_with(tmp_path: Path, monkeypatch) -> None:
    """`task_config.resolved` must describe the task as AUTHORED, not as rewritten.

    `run_task_internal_command` rewrites `driver: docker` -> `tempdir` before
    building the in-container Orchestrator — the one legitimate rewrite, since we
    are already inside the container the driver asked for. But the Orchestrator
    then recorded the REWRITTEN copy, so a docker run's own `task.json` claimed
    `driver: tempdir`.

    That fed straight into the gate that reads the driver back out of the record:
    `evaluate <run_dir>` on a container row skipped the host-grading refusal AND
    the `graded_on_host` stamp, and graded a container task against the host
    filesystem silently. Verified against a real docker run before the fix: a
    `driver: docker` task re-graded on the host, unprompted and unstamped.

    `recorded_task` is the seam, exercised here without needing docker.
    """
    from coder_eval.config import settings
    from coder_eval.models import (
        ApiBackend,
        FileExistsCriterion,
        SandboxConfig,
        TaskDefinition,
        parse_agent_config,
    )
    from coder_eval.orchestrator import Orchestrator

    monkeypatch.setattr(settings, "api_backend", ApiBackend.DIRECT)

    def _task(driver: str) -> TaskDefinition:
        return TaskDefinition(
            task_id="driver_record_probe",
            description="d",
            agent=parse_agent_config(type="none"),
            sandbox=SandboxConfig(driver=driver),
            success_criteria=[FileExistsCriterion(description="c", path="nope.txt")],
            pre_run=[],
            post_run=[],
        )

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    # Exactly the container's shape: RUN as tempdir, RECORD as docker.
    orch = Orchestrator(
        task=_task("tempdir"),
        recorded_task=_task("docker"),
        run_dir=run_dir,
        variant_id="v",
    )
    await orch.run()

    assert orch.result is not None
    assert orch.result.task_config is not None
    assert orch.result.task_config.resolved["sandbox"]["driver"] == "docker", (
        "the record denied the task ever used docker, which disarms the host-grading gate"
    )
    # The run itself really did use tempdir — the rewrite is not undone, only unrecorded.
    assert orch.task.sandbox.driver == "tempdir"


def test_the_recorded_task_defaults_to_the_task_being_run(tmp_path: Path) -> None:
    """Every caller but the in-container one passes nothing, and must be unaffected."""
    from coder_eval.models import FileExistsCriterion, SandboxConfig, TaskDefinition, parse_agent_config
    from coder_eval.orchestrator import Orchestrator

    task = TaskDefinition(
        task_id="t",
        description="d",
        agent=parse_agent_config(type="none"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(description="c", path="o")],
    )
    orch = Orchestrator(task=task, run_dir=tmp_path, variant_id="v")
    assert orch.recorded_task is task


# --------------------------------------------------------------------------
# `--workspace-dir` misuse is a clean CLI error, not a traceback
# --------------------------------------------------------------------------

_DOCKER_TASK = """task_id: t
description: d
initial_prompt: p
agent:
  type: claude-code
sandbox:
  driver: docker
  docker:
    image: some-image:latest
success_criteria:
  - type: file_exists
    path: proof.txt
    description: x
"""


def test_workspace_dir_with_docker_driver_is_a_clean_cli_error(tmp_path: Path) -> None:
    """run_batch's own guard raises a plain ValueError; the CLI must convert it
    to typer.BadParameter (exit 2, clean message) instead of an unhandled
    traceback -- --workspace-dir is not for sandbox.driver: docker tasks."""
    path = tmp_path / "t.yaml"
    path.write_text(_DOCKER_TASK, encoding="utf-8")

    result = runner.invoke(
        app, ["run", str(path), "--run-dir", str(tmp_path / "r"), "--workspace-dir", str(tmp_path / "ws")]
    )

    assert result.exit_code != 0
    # click.unstyle strips ANSI color codes -- CI renders Click's error box
    # with color (option names highlighted char-by-char), which would
    # otherwise split "--workspace-dir" across escape sequences and silently
    # break this check (see test_execute_format_harbor.py's equivalent).
    assert "--workspace-dir" in click.unstyle(result.output)
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------
# The empty-criteria guard checks post-`--resume` `to_run`, not the full
# `resolved` set -- an already-finalized row folded back from prior_results
# is never re-graded, so its own (possibly empty) criteria are moot.
# --------------------------------------------------------------------------


def test_empty_criteria_guard_ignores_an_already_finalized_resumed_row(tmp_path: Path) -> None:
    import typer

    from coder_eval.cli.run_command import _reject_empty_criteria_under_grade
    from coder_eval.models import ResolvedTask, TaskDefinition

    finalized_but_empty = ResolvedTask(
        task=TaskDefinition(
            task_id="already-done",
            description="d",
            agent=parse_agent_config(type=AgentKind.NONE),
            success_criteria=[],
        ),
        task_file=tmp_path / "t.yaml",
        run_dir=tmp_path,
        variant_id="v",
    )

    # The full `resolved` set (pre-resume) still refuses when actually graded
    # -- this is the control, proving the guard is not simply disabled.
    with pytest.raises(typer.BadParameter):
        _reject_empty_criteria_under_grade([finalized_but_empty], grade=True)

    # But `to_run` (post-resume) is what a real call site must pass: an
    # already-finalized row is peeled off by `_apply_resume` and folded back
    # from `prior_results` without being re-graded, so it is NOT in `to_run`
    # -- the empty `to_run` a resumed run would actually see must not raise.
    _reject_empty_criteria_under_grade([], grade=True)
