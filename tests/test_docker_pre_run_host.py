"""HARNESS-OUTSIDE: docker ``pre_run`` runs on the HOST before the container.

Under ``--driver docker`` the container runs the agent turn only; a task's
``pre_run`` runs host-side into a staging dir whose contents seed the container
workspace (mounted read-only, copied in by the in-container orchestrator after
template materialization, before the agent starts). These tests exercise the
host-side pieces with NO docker daemon:

* ``DockerRunner.run`` aborts BEFORE spawning a container when a
  ``fail_on_error`` pre_run fails, and records the failure on an ERROR result.
* ``Sandbox.seed_from`` copies both a ``seed.json`` file and a ``cp -r`` style
  directory tree into the sandbox, and wins over template starter collisions.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.models import (
    EvaluationResult,
    FinalStatus,
    PreRunCommand,
    ResolvedTask,
    SandboxConfig,
    TaskDefinition,
)
from coder_eval.sandbox import Sandbox


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="docker driver is POSIX-only")


def _make_rt(tmp_path: Path, task: TaskDefinition) -> ResolvedTask:
    task_dir = tmp_path / "taskdir"
    task_dir.mkdir(exist_ok=True)
    task_file = task_dir / "task.yaml"
    task_file.write_text("x", encoding="utf-8")
    return ResolvedTask(
        task=task,
        task_file=task_file,
        run_dir=tmp_path / "run",
        variant_id="v",
        source_yaml="raw",
    )


def _task(pre_run: list[PreRunCommand]) -> TaskDefinition:
    return TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x.txt"}],
        pre_run=pre_run,
    )


# --------------------------------------------------------------------------
# Sandbox.seed_from
# --------------------------------------------------------------------------


def _make_sandbox(tmp_path: Path) -> Sandbox:
    sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="t")
    workspace = tmp_path / "workspace"
    sandbox.setup(workspace)
    return sandbox


def test_seed_from_copies_seed_json_file(tmp_path):
    """A relative ``seed.json`` produced by pre_run lands in the sandbox."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "seed.json").write_text('{"id": 7}', encoding="utf-8")

    sandbox = _make_sandbox(tmp_path)
    copied = sandbox.seed_from(seed_dir)

    assert copied == 1
    assert (sandbox.sandbox_dir / "seed.json").read_text() == '{"id": 7}'


def test_seed_from_copies_directory_tree(tmp_path):
    """A ``cp -r _fixtures/<proj>`` style directory tree is copied recursively."""
    seed_dir = tmp_path / "seed"
    proj = seed_dir / "CodedAgent" / "src"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text("print('hi')", encoding="utf-8")
    (seed_dir / "CodedAgent" / "pyproject.toml").write_text("[project]", encoding="utf-8")

    sandbox = _make_sandbox(tmp_path)
    copied = sandbox.seed_from(seed_dir)

    assert copied >= 3  # 2 dirs + 2 files (CodedAgent, src, main.py, pyproject.toml)
    assert (sandbox.sandbox_dir / "CodedAgent" / "src" / "main.py").read_text() == "print('hi')"
    assert (sandbox.sandbox_dir / "CodedAgent" / "pyproject.toml").read_text() == "[project]"


def test_seed_wins_over_template_starter_collision(tmp_path):
    """A seed entry OVERWRITES a colliding pre-existing (template) file."""
    sandbox = _make_sandbox(tmp_path)
    # Simulate a template starter already present in the workspace.
    (sandbox.sandbox_dir / "config.txt").write_text("TEMPLATE", encoding="utf-8")

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "config.txt").write_text("SEED", encoding="utf-8")

    sandbox.seed_from(seed_dir)
    assert (sandbox.sandbox_dir / "config.txt").read_text() == "SEED"


def test_seed_dir_wins_over_template_file_collision(tmp_path):
    """Template left a plain FILE where the seed has a DIRECTORY → seed wins, no crash."""
    sandbox = _make_sandbox(tmp_path)
    # Template starter: a plain file named "data".
    (sandbox.sandbox_dir / "data").write_text("TEMPLATE-FILE", encoding="utf-8")

    seed_dir = tmp_path / "seed"
    (seed_dir / "data" / "nested").mkdir(parents=True)
    (seed_dir / "data" / "nested" / "f.txt").write_text("SEED", encoding="utf-8")

    sandbox.seed_from(seed_dir)  # must not raise FileExistsError
    dest = sandbox.sandbox_dir / "data"
    assert dest.is_dir()
    assert (dest / "nested" / "f.txt").read_text() == "SEED"


def test_seed_file_wins_over_template_dir_collision(tmp_path):
    """Template left a DIRECTORY where the seed has a FILE → seed wins, no crash."""
    sandbox = _make_sandbox(tmp_path)
    # Template starter: a directory named "config.txt" (contrived collision).
    (sandbox.sandbox_dir / "config.txt").mkdir()
    (sandbox.sandbox_dir / "config.txt" / "leftover").write_text("junk", encoding="utf-8")

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "config.txt").write_text("SEED", encoding="utf-8")

    sandbox.seed_from(seed_dir)  # must not raise
    dest = sandbox.sandbox_dir / "config.txt"
    assert dest.is_file()
    assert dest.read_text() == "SEED"


def test_seed_from_skips_escaping_symlink(tmp_path):
    """A seed symlink whose target escapes the seed tree is skipped with a warning."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    # Absolute escape.
    (seed_dir / "abs_escape").symlink_to("/etc/passwd")
    # Relative escape (../../ climbs out of seed_dir).
    (seed_dir / "rel_escape").symlink_to("../../outside")

    sandbox = _make_sandbox(tmp_path)
    copied = sandbox.seed_from(seed_dir)

    assert copied == 0
    assert not (sandbox.sandbox_dir / "abs_escape").exists()
    assert not (sandbox.sandbox_dir / "rel_escape").is_symlink()


def test_seed_from_preserves_within_tree_symlink(tmp_path):
    """A within-tree relative symlink (target stays inside the seed) is preserved as a link."""
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "real.txt").write_text("payload", encoding="utf-8")
    (seed_dir / "link.txt").symlink_to("real.txt")  # relative, non-escaping

    sandbox = _make_sandbox(tmp_path)
    sandbox.seed_from(seed_dir)

    dest_link = sandbox.sandbox_dir / "link.txt"
    assert dest_link.is_symlink()
    assert os.readlink(dest_link) == "real.txt"
    assert dest_link.read_text() == "payload"


def test_seed_from_empty_or_missing_is_noop(tmp_path):
    """A missing seed dir copies nothing."""
    sandbox = _make_sandbox(tmp_path)
    assert sandbox.seed_from(tmp_path / "nope") == 0


def test_seed_from_covers_workspace_dir_mode(tmp_path):
    """seed_from keys off sandbox_dir, so a workspace_dir-style target works too."""
    sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="t")
    workdir = tmp_path / "root_workdir"  # stand-in for a docker WORKDIR capture
    sandbox.setup(workdir)

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "seed.json").write_text("{}", encoding="utf-8")

    sandbox.seed_from(seed_dir)
    assert (workdir / "seed.json").is_file()


# --------------------------------------------------------------------------
# DockerRunner.run host-side pre_run abort
# --------------------------------------------------------------------------


async def test_fail_on_error_pre_run_aborts_before_container(tmp_path):
    """A fail_on_error pre_run failure aborts BEFORE docker run and records ERROR."""
    from coder_eval.isolation import docker_runner
    from coder_eval.isolation.docker_runner import DockerRunner

    task = _task([PreRunCommand(command="exit 3", fail_on_error=True)])
    rt = _make_rt(tmp_path, task)
    runner = DockerRunner(rt)

    # Stub out everything up to (and including) _prepare_host_mounts so run()
    # reaches the pre_run block without touching a docker daemon.
    with (
        patch.object(docker_runner, "_preflight", return_value=None),
        patch.object(DockerRunner, "_build_image", return_value="img"),
        patch.object(docker_runner, "_preflight_image_version", return_value=None),
        patch.object(docker_runner, "_resolve_workspace_dir", return_value=None),
        patch.object(DockerRunner, "_stage_inputs", new=AsyncMock(return_value=None)),
        patch.object(DockerRunner, "_prepare_host_mounts", return_value=None),
        # These must NOT be reached: the abort happens before the container.
        patch.object(DockerRunner, "_build_argv") as build_argv,
        patch("asyncio.create_subprocess_exec") as spawn,
    ):
        result: EvaluationResult = await runner.run()

    build_argv.assert_not_called()
    spawn.assert_not_called()
    assert result.final_status == FinalStatus.ERROR
    assert len(result.pre_run_results) == 1
    assert result.pre_run_results[0].exit_code == 3


async def test_pre_run_abort_runs_post_run_teardown_over_seed_dir(tmp_path):
    """When a fail_on_error pre_run aborts before the container, post_run teardown
    STILL runs host-side over the seed dir (cloud-resource cleanup parity with the
    tempdir orchestrator's finally-runs-post_run). Assert post_run ran with
    cwd=seed_dir (it sees the seed.json a prior pre_run wrote) and staging is
    cleaned up."""
    from coder_eval.isolation import docker_runner
    from coder_eval.isolation.docker_runner import DockerRunner
    from coder_eval.models import PostRunCommand

    # pre_run: write seed.json, THEN a fail_on_error command that fails.
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x.txt"}],
        pre_run=[
            PreRunCommand(command='printf %s \'{"resource": "abc"}\' > seed.json', fail_on_error=True),
            PreRunCommand(command="exit 5", fail_on_error=True),
        ],
        # post_run records its cwd and proves it can read the seeded seed.json.
        post_run=[PostRunCommand(command="pwd && cat seed.json")],
    )
    rt = _make_rt(tmp_path, task)
    runner = DockerRunner(rt)

    captured_staging: dict[str, Path] = {}
    real_rmtree = docker_runner.shutil.rmtree

    def _capture_rmtree(path, *args, **kwargs):
        captured_staging["staging"] = Path(path)
        return real_rmtree(path, *args, **kwargs)

    with (
        patch.object(docker_runner, "_preflight", return_value=None),
        patch.object(DockerRunner, "_build_image", return_value="img"),
        patch.object(docker_runner, "_preflight_image_version", return_value=None),
        patch.object(docker_runner, "_resolve_workspace_dir", return_value=None),
        patch.object(DockerRunner, "_stage_inputs", new=AsyncMock(return_value=None)),
        patch.object(DockerRunner, "_prepare_host_mounts", return_value=None),
        patch.object(DockerRunner, "_build_argv") as build_argv,
        patch("asyncio.create_subprocess_exec") as spawn,
        patch.object(docker_runner.shutil, "rmtree", side_effect=_capture_rmtree),
    ):
        result: EvaluationResult = await runner.run()

    # Aborted before the container.
    build_argv.assert_not_called()
    spawn.assert_not_called()
    assert result.final_status == FinalStatus.ERROR

    # post_run teardown WAS invoked (over the seed dir): its stdout carries the
    # cwd (.../workspace_seed) and the seed.json it read there.
    assert len(result.post_run_results) == 1
    out = result.post_run_results[0].stdout
    assert "workspace_seed" in out
    assert '"resource": "abc"' in out

    # Staging dir is cleaned up on this path.
    staging = captured_staging["staging"]
    assert not staging.exists()


async def test_seed_round_trips_to_copied_out_workspace_for_post_run(tmp_path):
    """A seed lands in the DIRECT_WRITE workspace (= the copied-out artifacts
    dir), so the host ``post_run`` teardown sees ``seed.json`` — not just the
    agent. Exercises the full seam: seed_from writes into sandbox_dir, and the
    same dir is what ``run_post_run_on_host`` runs over."""
    from coder_eval.isolation.docker_runner import run_post_run_on_host
    from coder_eval.models import PostRunCommand

    # DIRECT_WRITE artifacts dir the container would write into and the host
    # would then copy out / grade / tear down over.
    artifacts = tmp_path / "run" / "artifacts" / "t"
    artifacts.mkdir(parents=True)

    # In-container: pre_run seeded this dir with seed.json (via seed_from).
    sandbox = Sandbox(SandboxConfig(driver="tempdir"), task_id="t")
    sandbox.setup(artifacts)
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "seed.json").write_text('{"resource": "xyz"}', encoding="utf-8")
    sandbox.seed_from(seed_dir)

    # Host: post_run teardown reads the seed off the copied-out workspace.
    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="docker"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x", "weight": 0.0}],
        post_run=[PostRunCommand(command="cat seed.json")],
    )
    rt = _make_rt(tmp_path, task)
    result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        sandbox_path=str(artifacts),
        success_criteria_results=[],
    )
    await run_post_run_on_host(result, rt)
    assert '"resource": "xyz"' in result.post_run_results[0].stdout


# --------------------------------------------------------------------------
# runs_in: host | agent split
# --------------------------------------------------------------------------


async def test_stage_inputs_context_flags_for_agent_pre_run(tmp_path):
    """A task with a ``runs_in: agent`` pre_run stages context.json with
    ``pre_run_in_container=True`` and ``skip_pre_post_commands=False`` (the
    container runs the ``agent`` subset)."""
    from coder_eval.isolation.docker_runner import DockerRunner

    task = _task(
        [
            PreRunCommand(command="cp -r fixtures ."),  # host (default)
            PreRunCommand(command="uv sync", runs_in="agent"),
        ]
    )
    rt = _make_rt(tmp_path, task)
    runner = DockerRunner(rt)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    await runner._stage_inputs(input_dir)

    import json

    ctx = json.loads((input_dir / "context.json").read_text())
    assert ctx["pre_run_in_container"] is True
    assert ctx["skip_pre_post_commands"] is False
    assert ctx["workspace_seed_dir"] is not None  # seed mount present (host subset seeds it)


async def test_stage_inputs_context_flags_for_host_only_pre_run(tmp_path):
    """A task with only ``runs_in: host`` (default) pre_run stages context.json
    with ``skip_pre_post_commands=True`` (blanket suppress; nothing runs in the
    container) and ``pre_run_in_container=False`` — Stage-2 behavior preserved."""
    from coder_eval.isolation.docker_runner import DockerRunner

    task = _task(
        [
            PreRunCommand(command="python seed.py"),
            PreRunCommand(command="cp -r fixtures ."),
        ]
    )
    rt = _make_rt(tmp_path, task)
    runner = DockerRunner(rt)
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    await runner._stage_inputs(input_dir)

    import json

    ctx = json.loads((input_dir / "context.json").read_text())
    assert ctx["skip_pre_post_commands"] is True
    assert ctx["pre_run_in_container"] is False


async def test_host_runs_only_host_subset_of_pre_run(tmp_path):
    """The host pre_run step runs ONLY the ``runs_in: host`` subset. The
    ``runs_in: agent`` command is NOT run host-side (it would run in-container).
    Asserted via the recording file each command writes into the seed dir."""
    from coder_eval.isolation import docker_runner
    from coder_eval.isolation.docker_runner import DockerRunner

    task = _task(
        [
            PreRunCommand(command="printf host > host_marker.txt"),  # runs_in: host
            PreRunCommand(command="printf agent > agent_marker.txt", runs_in="agent"),
        ]
    )
    rt = _make_rt(tmp_path, task)
    runner = DockerRunner(rt)

    captured: dict[str, Path] = {}
    real_rmtree = docker_runner.shutil.rmtree

    def _capture_rmtree(path, *args, **kwargs):
        # Snapshot the seed dir contents BEFORE staging is removed.
        seed = Path(path) / "workspace_seed"
        if seed.is_dir():
            captured["host_marker"] = seed / "host_marker.txt"
            captured["agent_marker"] = seed / "agent_marker.txt"
            captured["host_exists"] = (seed / "host_marker.txt").exists()  # type: ignore[assignment]
            captured["agent_exists"] = (seed / "agent_marker.txt").exists()  # type: ignore[assignment]
        return real_rmtree(path, *args, **kwargs)

    async def _fake_stream(self, proc, log_fh):
        return 0

    async def _fake_parse(self, output_dir, returncode, log_path):
        return EvaluationResult(
            task_id="t",
            task_description="d",
            variant_id="v",
            agent_type="claude-code",
            started_at=datetime.now(),
            final_status=FinalStatus.SUCCESS,
            iteration_count=1,
        )

    class _FakeProc:
        returncode = 0

        async def wait(self):
            return 0

    with (
        patch.object(docker_runner, "_preflight", return_value=None),
        patch.object(DockerRunner, "_build_image", return_value="img"),
        patch.object(docker_runner, "_preflight_image_version", return_value=None),
        patch.object(docker_runner, "_resolve_workspace_dir", return_value=None),
        patch.object(DockerRunner, "_stage_inputs", new=AsyncMock(return_value=None)),
        patch.object(DockerRunner, "_prepare_host_mounts", return_value=None),
        patch.object(DockerRunner, "_build_argv", return_value=["true"]),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_FakeProc())),
        patch.object(DockerRunner, "_stream_container_output", new=_fake_stream),
        patch.object(DockerRunner, "_parse_result_or_raise", new=_fake_parse),
        patch.object(docker_runner.shutil, "rmtree", side_effect=_capture_rmtree),
    ):
        await runner.run()

    assert captured["host_exists"] is True
    assert captured["agent_exists"] is False


def _orch_with(tmp_path: Path, pre_run, post_run=None, **kwargs):
    """Build an Orchestrator with a fake sandbox + result so the real
    ``_run_pre_run_commands`` / ``_run_post_run_commands`` execute against a
    patched ``run_command_list`` (which records exactly what was dispatched)."""
    from coder_eval.orchestrator import Orchestrator

    task = TaskDefinition(
        task_id="t",
        description="d",
        initial_prompt="p",
        sandbox=SandboxConfig(driver="tempdir"),
        agent={"type": "claude-code"},
        success_criteria=[{"type": "file_exists", "description": "c", "path": "x.txt"}],
        pre_run=pre_run,
        post_run=post_run or [],
    )
    orch = Orchestrator(
        task=task,
        run_dir=tmp_path / "run",
        task_file=tmp_path / "task.yaml",
        variant_id="v",
        **kwargs,
    )
    orch.result = EvaluationResult(
        task_id="t",
        task_description="d",
        variant_id="v",
        agent_type="claude-code",
        started_at=datetime.now(),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
    )
    # Minimal fake sandbox so _run_command_list doesn't early-return.
    from types import SimpleNamespace

    orch.sandbox = SimpleNamespace(sandbox_dir=tmp_path / "sb")  # type: ignore[assignment]
    return orch


async def test_in_container_orchestrator_runs_only_agent_subset(tmp_path):
    """The in-container orchestrator (pre_run_in_container=True) runs ONLY the
    ``runs_in: agent`` pre_run subset and skips ALL post_run — via the REAL
    _run_pre_run_commands / _run_post_run_commands against a recording stub."""
    from coder_eval import orchestrator as orch_mod
    from coder_eval.models import PostRunCommand

    orch = _orch_with(
        tmp_path,
        pre_run=[
            PreRunCommand(command="host cmd"),  # runs_in: host — must NOT run in-container
            PreRunCommand(command="agent cmd", runs_in="agent"),
        ],
        post_run=[PostRunCommand(command="teardown")],
        pre_run_in_container=True,
    )

    dispatched: list[list[str]] = []

    async def _record(commands, results, label, **kwargs):
        dispatched.append([c.command for c in commands])

    with patch.object(orch_mod, "run_command_list", new=_record):
        await orch._run_pre_run_commands()
        await orch._run_post_run_commands()  # must be a no-op (post is host-only)

    assert dispatched == [["agent cmd"]]  # only the agent subset; post_run never dispatched


async def test_tempdir_runs_all_pre_run_and_ignores_runs_in(tmp_path):
    """Under tempdir (both flags False), the FULL pre_run runs in-process AND
    post_run runs — ``runs_in: agent`` behaves identically to ``host``."""
    from coder_eval import orchestrator as orch_mod
    from coder_eval.models import PostRunCommand

    orch = _orch_with(
        tmp_path,
        pre_run=[
            PreRunCommand(command="host cmd"),
            PreRunCommand(command="agent cmd", runs_in="agent"),
        ],
        post_run=[PostRunCommand(command="teardown")],
    )

    dispatched: list[list[str]] = []

    async def _record(commands, results, label, **kwargs):
        dispatched.append([c.command for c in commands])

    with patch.object(orch_mod, "run_command_list", new=_record):
        await orch._run_pre_run_commands()
        await orch._run_post_run_commands()

    assert dispatched == [["host cmd", "agent cmd"], ["teardown"]]
