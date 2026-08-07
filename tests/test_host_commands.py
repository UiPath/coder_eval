"""Unit tests for the extracted :func:`run_command_list` free function.

The pre_run/post_run execution loop was extracted from
``Orchestrator._run_command_list`` into ``evaluation/host_commands.py`` so the
docker host-side path can reuse it without an Orchestrator. These tests pin the
semantics DIRECTLY on the free function: ``PreRunCommand.fail_on_error`` abort,
``PostRunCommand`` informational-continue, per-command timeout, output
truncation, and a caller-supplied ``cwd``. (The Orchestrator delegation is
covered end-to-end by ``test_pre_run.py`` / ``test_post_run.py``.)
"""

from __future__ import annotations

import pytest

from coder_eval.evaluation.host_commands import DEFAULT_MAX_OUTPUT, run_command_list
from coder_eval.models import PostRunCommand, PostRunResult, PreRunCommand


async def test_pre_run_fail_on_error_aborts(tmp_path):
    """A PreRunCommand with fail_on_error=True raises and stops the loop."""
    results: list[PostRunResult] = []
    cmds = [
        PreRunCommand(command="exit 3"),  # fail_on_error defaults True
        PreRunCommand(command="echo should-not-run"),
    ]
    with pytest.raises(RuntimeError, match="Pre-run command failed"):
        await run_command_list(cmds, results, "pre_run", cwd=tmp_path)
    # Only the failing command captured; the loop aborted before the second.
    assert len(results) == 1
    assert results[0].exit_code == 3


async def test_pre_run_fail_on_error_false_continues(tmp_path):
    """fail_on_error=False records the failure but keeps going."""
    results: list[PostRunResult] = []
    cmds = [
        PreRunCommand(command="exit 1", fail_on_error=False),
        PreRunCommand(command="echo ran", fail_on_error=False),
    ]
    await run_command_list(cmds, results, "pre_run", cwd=tmp_path)
    assert len(results) == 2
    assert results[0].exit_code == 1
    assert results[1].stdout.strip() == "ran"


async def test_post_run_informational_continue(tmp_path):
    """PostRunCommand never aborts on failure (no fail_on_error field)."""
    results: list[PostRunResult] = []
    cmds = [
        PostRunCommand(command="exit 7"),
        PostRunCommand(command="echo after"),
    ]
    await run_command_list(cmds, results, "post_run", cwd=tmp_path)
    assert len(results) == 2
    assert results[0].exit_code == 7
    assert results[1].stdout.strip() == "after"


async def test_timeout_recorded_and_non_fatal_for_post(tmp_path):
    results: list[PostRunResult] = []
    await run_command_list([PostRunCommand(command="sleep 5", timeout=1)], results, "post_run", cwd=tmp_path)
    assert len(results) == 1
    assert results[0].exit_code is None
    assert "Timed out" in (results[0].error or "")


async def test_timeout_aborts_for_pre_when_fail_on_error(tmp_path):
    results: list[PostRunResult] = []
    with pytest.raises(RuntimeError, match="timed out after 1s"):
        await run_command_list([PreRunCommand(command="sleep 5", timeout=1)], results, "pre_run", cwd=tmp_path)
    assert results[0].error is not None


async def test_output_truncated(tmp_path):
    results: list[PostRunResult] = []
    cmd = PostRunCommand(command="python3 -c \"print('x' * 200_000)\"")
    await run_command_list([cmd], results, "post_run", cwd=tmp_path, max_output=DEFAULT_MAX_OUTPUT)
    assert len(results[0].stdout) <= DEFAULT_MAX_OUTPUT


async def test_cwd_honored(tmp_path):
    """Commands run with the caller-supplied cwd, not the process cwd."""
    workdir = tmp_path / "seed"
    workdir.mkdir()
    results: list[PostRunResult] = []
    await run_command_list(
        [PostRunCommand(command='python3 -c "import os; print(os.getcwd())"')],
        results,
        "post_run",
        cwd=workdir,
    )
    assert results[0].stdout.strip() == str(workdir)


async def test_cwd_accepts_str(tmp_path):
    # Prove run_command_list accepts a str cwd AND runs the command there by the
    # file it creates in the working dir. Asserting the file lands in tmp_path is
    # cross-platform; parsing `pwd` output is not (git-bash on Windows reports a
    # POSIX-style `/c/Users/...` for a `C:\Users\...` cwd, so a raw Path compare
    # fails there).
    results: list[PostRunResult] = []
    await run_command_list(
        [PostRunCommand(command="echo ran > cwd_marker.txt")],
        results,
        "post_run",
        cwd=str(tmp_path),
    )
    assert (tmp_path / "cwd_marker.txt").is_file()


async def test_empty_command_list_noop(tmp_path):
    results: list[PostRunResult] = []
    await run_command_list([], results, "post_run", cwd=tmp_path)
    assert results == []
