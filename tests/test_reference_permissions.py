"""Tests for the anti-cheat permission window around agent turns.

The agent under evaluation shares a filesystem with the harness, so without an
active control it can read the reference solution instead of solving the task.
``fs_permissions.set_permissions`` chmods the reference and task
directories to 000 for the duration of every ``agent.communicate`` call.
"""

import asyncio
import contextlib
import os
import stat
from pathlib import Path

import pytest

from coder_eval.fs_permissions import READ_ONLY_MODE, RESTRICTED_MODE, set_permissions


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def guarded_dir(tmp_path):
    """A directory with a file in it, guaranteed restored even if a test fails."""
    d = tmp_path / "secret"
    d.mkdir()
    (d / "answer.txt").write_text("SOLUTION=42", encoding="utf-8")
    original = _mode(d)
    yield d
    os.chmod(d, original)


class TestRestrictPermissions:
    async def test_restricts_then_restores(self, guarded_dir):
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_contents_unreadable_inside_the_window(self, guarded_dir):
        """The point of the exercise: the solution is not readable during a turn."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses DAC; the docker path drops DAC_* caps instead")

        async with set_permissions([guarded_dir]):
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

        assert (guarded_dir / "answer.txt").read_text(encoding="utf-8") == "SOLUTION=42"

    async def test_restores_on_exception(self, guarded_dir):
        """An agent crash or turn timeout must not leave the tree unreadable."""
        original = _mode(guarded_dir)

        # Explicit try/except rather than `pytest.raises`: CodeQL cannot model
        # pytest.raises as catching, so it reports everything after such a block
        # as unreachable (alerts 77/80 on the first revision of this file).
        raised = False
        try:
            async with set_permissions([guarded_dir]):
                raise RuntimeError("agent crashed")
        except RuntimeError:
            raised = True
        assert raised, "the exception must propagate, not be swallowed by the CM"
        assert _mode(guarded_dir) == original

    async def test_restores_on_cancellation(self, guarded_dir):
        """The task_timeout watchdog cancels the orchestrator task mid-turn."""
        original = _mode(guarded_dir)
        entered = asyncio.Event()

        async def _body():
            async with set_permissions([guarded_dir]):
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(_body())
        await entered.wait()
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled, "the task must actually be cancelled"
        assert _mode(guarded_dir) == original

    async def test_preserves_a_non_default_original_mode(self, guarded_dir):
        """Restore the mode we observed, not a hardcoded 0o755.

        0o700 rather than a group-readable mode: it makes the same point (the
        pre-window mode is captured, not assumed) without tripping the
        overly-permissive-chmod scanner.
        """
        os.chmod(guarded_dir, 0o700)

        async with set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == 0o700

    async def test_none_entries_and_duplicates_are_tolerated(self, guarded_dir):
        """Callers pass [task_dir, reference_dir] without pre-filtering."""
        original = _mode(guarded_dir)

        async with set_permissions([None, guarded_dir, guarded_dir, None]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_missing_path_is_a_no_op(self, tmp_path):
        async with set_permissions([tmp_path / "does_not_exist"]):
            pass  # must not raise

    async def test_chmod_refusal_warns_and_skips_the_matching_pop(self, guarded_dir, monkeypatch, caplog):
        """The branch whose entire job is to signal "this run is NOT protected".

        A read-only mount or a foreign owner makes chmod fail; the window then
        does not apply and the run continues unprotected, so the warning is the
        only signal an operator gets. Exiting must not chmod either — a pop with
        no matching push would apply a mode nobody asked for.
        """
        import coder_eval.fs_permissions as perms

        original = _mode(guarded_dir)
        calls: list[tuple[str, int]] = []

        def _refuse(path, mode):
            calls.append((str(path), mode))
            raise PermissionError(30, "Read-only file system")

        monkeypatch.setattr(perms.os, "chmod", _refuse)
        with caplog.at_level("WARNING"):
            async with set_permissions([guarded_dir]):
                pass

        assert "could not chmod" in caplog.text
        assert len(calls) == 1, "a refused push must not be followed by a pop"
        monkeypatch.undo()
        assert _mode(guarded_dir) == original

    async def test_unresolvable_path_is_warned_not_silently_dropped(self, tmp_path, monkeypatch, caplog):
        import coder_eval.fs_permissions as perms

        def _boom(self, *a, **k):
            raise OSError("nope")

        monkeypatch.setattr(perms.Path, "resolve", _boom)
        with caplog.at_level("WARNING"):
            async with set_permissions([tmp_path]):
                pass
        assert "could not resolve" in caplog.text

    async def test_overlapping_windows_of_the_same_mode(self, guarded_dir):
        """Two windows applying the same mode: the inner exit must NOT restore.

        This is what a refcount used to buy; the stack gives it for free, because
        the inner pop re-applies the outer's (identical) mode.
        """
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir]):
            async with set_permissions([guarded_dir]):
                assert _mode(guarded_dir) == RESTRICTED_MODE
            # Inner exited; outer still holds it.
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nested_regrant_falls_back_to_the_enclosing_mode(self, guarded_dir):
        """The whole reason this is a stack and not a refcount.

        Code that runs INSIDE the turn but is not the agent (a future live
        criterion, say) opens a read-only window over a shielded path. Exiting it
        must return to the enclosing 000 — not to the pre-window mode, which would
        silently un-shield the reference for the rest of the agent's turn.
        """
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            assert _mode(guarded_dir) == RESTRICTED_MODE

            async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert _mode(guarded_dir) == READ_ONLY_MODE

            # Back to the agent-facing window, NOT to `original`.
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nested_regrant_actually_permits_reads(self, guarded_dir):
        """The mode is not just bookkeeping — the re-grant really opens the file."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses DAC; the docker path drops DAC_* caps instead")

        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

            async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert (guarded_dir / "answer.txt").read_text(encoding="utf-8") == "SOLUTION=42"

            # Re-shielded for the remainder of the turn.
            with pytest.raises(PermissionError):
                (guarded_dir / "answer.txt").read_text(encoding="utf-8")

    async def test_regrant_unwinds_on_exception(self, guarded_dir):
        """A raise inside the re-grant must not leave the path readable."""
        async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            raised = False
            try:
                async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                    raise RuntimeError("live check blew up")
            except RuntimeError:
                raised = True
            assert raised
            assert _mode(guarded_dir) == RESTRICTED_MODE

    async def test_three_deep_stack_unwinds_in_order(self, guarded_dir):
        original = _mode(guarded_dir)

        async with set_permissions([guarded_dir], mode=0o700):
            async with set_permissions([guarded_dir], mode=RESTRICTED_MODE):
                async with set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                    assert _mode(guarded_dir) == READ_ONLY_MODE
                assert _mode(guarded_dir) == RESTRICTED_MODE
            assert _mode(guarded_dir) == 0o700

        assert _mode(guarded_dir) == original

    async def test_concurrent_holders_restore_exactly_once(self, guarded_dir):
        """Same invariant, driven concurrently rather than lexically nested."""
        original = _mode(guarded_dir)
        release = asyncio.Event()

        async def _hold():
            async with set_permissions([guarded_dir]):
                await release.wait()

        holders = [asyncio.create_task(_hold()) for _ in range(5)]
        await asyncio.sleep(0.05)
        assert _mode(guarded_dir) == RESTRICTED_MODE

        release.set()
        await asyncio.gather(*holders)
        assert _mode(guarded_dir) == original

    async def test_out_of_order_release_of_differing_modes(self, guarded_dir):
        """Pins the documented single-holder assumption.

        `pop()` discards `applied[-1]` regardless of which window is exiting, so
        two OVERLAPPING (not nested) windows at different modes released in push
        order do not restore per-holder. The orchestrator never does this — the
        window is container-only and one task per container — but the primitive
        is public, so the behaviour is pinned rather than left to chance.
        """
        original = _mode(guarded_dir)

        outer = set_permissions([guarded_dir], mode=RESTRICTED_MODE)
        inner = set_permissions([guarded_dir], mode=READ_ONLY_MODE)
        await outer.__aenter__()
        await inner.__aenter__()
        assert _mode(guarded_dir) == READ_ONLY_MODE

        # Release in PUSH order (not LIFO): the stack pops the top entry.
        await outer.__aexit__(None, None, None)
        assert _mode(guarded_dir) == RESTRICTED_MODE
        await inner.__aexit__(None, None, None)

        assert _mode(guarded_dir) == original

    async def test_same_path_via_different_routes_shares_one_entry(self, guarded_dir):
        """Refcount keys are resolved paths, so `d` and `d/../d` are one entry."""
        original = _mode(guarded_dir)
        indirect = guarded_dir.parent / ".." / guarded_dir.parent.name / guarded_dir.name

        async with set_permissions([guarded_dir]):
            async with set_permissions([indirect]):
                assert _mode(guarded_dir) == RESTRICTED_MODE
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original


class TestSandboxDriverGate:
    """`Sandbox.set_permissions` decides whether a chmod window applies at all."""

    def _sandbox(self, tmp_path, *, driver="tempdir"):
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox

        return Sandbox(SandboxConfig(driver=driver, python=None), task_id="t", task_dir=tmp_path)

    def test_host_run_does_not_enforce(self, tmp_path, monkeypatch):
        """On the host the agent is just another process with our uid, and
        `tasks/` is shared across a parallel batch — chmod buys nothing and has
        cross-task side effects on the user's working copy."""
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        assert self._sandbox(tmp_path).enforces_permission_windows is False

    def test_in_container_enforces_even_though_driver_reads_tempdir(self, tmp_path, monkeypatch):
        """REGRESSION GUARD for a silent-disable trap.

        `run_task_internal_command` rewrites `driver: docker` -> `tempdir` before
        constructing the Orchestrator, because nested docker is impossible in the
        image. So inside the container the driver reads "tempdir". Gating on the
        driver would therefore disable the anti-cheat window on exactly the path
        that needs it — the gate must key on CODER_EVAL_IN_CONTAINER instead.
        """
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        sandbox = self._sandbox(tmp_path, driver="tempdir")
        assert sandbox.config.driver == "tempdir"
        assert sandbox.enforces_permission_windows is True

    async def test_no_op_on_host_leaves_mode_untouched(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.delenv("CODER_EVAL_IN_CONTAINER", raising=False)
        original = _mode(guarded_dir)

        async with self._sandbox(tmp_path).set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == original  # untouched

        assert _mode(guarded_dir) == original

    async def test_applies_in_container(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        original = _mode(guarded_dir)

        async with self._sandbox(tmp_path).set_permissions([guarded_dir]):
            assert _mode(guarded_dir) == RESTRICTED_MODE

        assert _mode(guarded_dir) == original

    async def test_nesting_still_works_through_the_sandbox(self, guarded_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
        sandbox = self._sandbox(tmp_path)

        async with sandbox.set_permissions([guarded_dir], mode=RESTRICTED_MODE):
            async with sandbox.set_permissions([guarded_dir], mode=READ_ONLY_MODE):
                assert _mode(guarded_dir) == READ_ONLY_MODE
            assert _mode(guarded_dir) == RESTRICTED_MODE


class TestOrchestratorWiring:
    """End-to-end: the agent cannot read the reference during its own turn."""

    async def test_agent_cannot_read_reference_mid_turn(self, tmp_path, monkeypatch):
        """The regression this whole feature exists for.

        Drives the real ``_communicate_with_retry`` seam with an agent that tries
        to read the reference solution from inside ``communicate`` — exactly what a
        cheating agent does — and asserts it is denied, while the same read succeeds
        once the turn is over.
        """
        if os.geteuid() == 0:
            pytest.skip("root bypasses DAC; the docker path drops DAC_* caps instead")

        from unittest.mock import MagicMock

        from coder_eval.models import EvaluationResult, FinalStatus, TurnRecord
        from coder_eval.orchestrator import Orchestrator

        task_dir = tmp_path / "task"
        reference = task_dir / "reference"
        reference.mkdir(parents=True)
        (reference / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(
            task=task,
            run_dir=tmp_path / "run",
            variant_id="v",
            task_file=task_file,
        )
        await orchestrator._stage_reference()
        staged = orchestrator._reference_dir
        assert staged is not None

        orchestrator.sandbox = _container_sandbox(task_dir, tmp_path / "sbx", monkeypatch)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        observed: dict[str, object] = {}

        async def _cheating_communicate(prompt, **kwargs):
            observed["reference"] = _try_read(staged / "solution.py")
            observed["task_dir"] = _try_read(task_file)  # deliberately NOT shielded
            return TurnRecord(iteration=1, prompt=prompt, user_input=prompt, agent_output="done")

        agent = MagicMock()
        agent.communicate = _cheating_communicate
        agent.pending_turn = None
        orchestrator.agent = agent

        await orchestrator._communicate_with_retry(prompt="go", iteration=1, operation_label="test")

        # The reference is denied during the turn...
        assert observed["reference"] == "DENIED"
        # ...while the task dir deliberately is NOT shielded: under docker it is a
        # `:ro` mount (chmod -> EROFS) and the same YAML is readable at
        # /work/input regardless, so shielding it bought nothing but a per-turn
        # warning. Pinned so re-adding it is a conscious decision.
        assert observed["task_dir"] == "# task"
        # ...and readable again afterwards, so criteria and judges still work.
        assert (staged / "solution.py").read_text(encoding="utf-8") == "SOLUTION=42"
        assert task_file.read_text(encoding="utf-8") == "# task"

    async def test_reference_is_unshielded_by_the_time_criteria_run(self, tmp_path, monkeypatch):
        """Criteria and judges must see a READABLE reference.

        The window closes when ``_communicate_with_retry`` returns, so every
        ``check_all_async`` call site sits outside it. That ordering is easy to
        break by widening the ``async with`` — this test asserts the observable
        mode at the moment criteria actually run, not just the call structure.
        """
        from unittest.mock import AsyncMock, MagicMock

        from coder_eval.models import CriterionResult, EvaluationResult, FinalStatus, TurnRecord
        from coder_eval.orchestrator import Orchestrator

        task_dir = tmp_path / "task"
        reference = task_dir / "reference"
        reference.mkdir(parents=True)
        (reference / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="v", task_file=task_file)
        await orchestrator._stage_reference()
        staged = orchestrator._reference_dir
        assert staged is not None

        orchestrator.sandbox = _container_sandbox(task_dir, tmp_path / "sbx", monkeypatch)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        seen: dict[str, object] = {}

        async def _communicate(prompt, **kwargs):
            seen["during_turn"] = _mode(staged)
            return TurnRecord(iteration=1, prompt=prompt, user_input=prompt, agent_output="done")

        agent = MagicMock()
        agent.communicate = _communicate
        agent.pending_turn = None
        orchestrator.agent = agent

        async def _check_all_async(*args, **kwargs):
            seen["during_criteria"] = _mode(staged)
            # The judge reads reference files off disk — prove that works here.
            seen["content"] = (staged / "solution.py").read_text(encoding="utf-8")
            return [CriterionResult(criterion_type="file_exists", description="x", score=1.0)]

        orchestrator.success_checker = MagicMock()
        orchestrator.success_checker.check_all_async = AsyncMock(side_effect=_check_all_async)

        await orchestrator._evaluation_loop()

        assert seen["during_turn"] == RESTRICTED_MODE
        assert seen["during_criteria"] != RESTRICTED_MODE
        assert seen["content"] == "SOLUTION=42"


def _container_sandbox(task_dir: Path, sandbox_dir: Path, monkeypatch) -> "object":
    """A real Sandbox that reports itself as running inside a docker container.

    `enforces_permission_windows` keys on CODER_EVAL_IN_CONTAINER rather than
    `config.driver`, precisely because the in-container entry point rewrites
    the driver to "tempdir" — so this fixture mirrors production by leaving the
    driver at "tempdir" and setting only the env var.
    """
    from coder_eval.models import SandboxConfig
    from coder_eval.sandbox import Sandbox

    monkeypatch.setenv("CODER_EVAL_IN_CONTAINER", "1")
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(SandboxConfig(driver="tempdir", python=None), task_id="anti_cheat", task_dir=task_dir)
    sandbox.sandbox_dir = sandbox_dir
    return sandbox


def _try_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError):
        return "DENIED"


def _reference_task(*, reference_dir: str):
    from coder_eval.models import (
        FileExistsCriterion,
        ReferenceSource,
        SandboxConfig,
        TaskDefinition,
        parse_agent_config,
    )

    return TaskDefinition(
        task_id="anti_cheat",
        description="d",
        initial_prompt="do it",
        agent=parse_agent_config(type="claude-code"),
        sandbox=SandboxConfig(driver="tempdir"),
        success_criteria=[FileExistsCriterion(path="x.py", description="x")],
        reference=ReferenceSource(directory=reference_dir),
    )


class TestSetupWiring:
    """Guards that the feature is actually armed by _setup, not just callable."""

    async def test_setup_stages_the_reference_and_binds_it_to_the_sandbox(self, tmp_path, monkeypatch):
        """Without this, deleting the `_stage_reference()` call from `_setup` keeps
        the whole suite green while every real run has `_reference_dir is None` —
        no REFERENCE_DIR, no shielded path, judges grading with no reference.
        Every other test in this file calls `_stage_reference()` by hand.
        """
        from unittest.mock import AsyncMock, patch

        from coder_eval.models import EvaluationResult, FinalStatus
        from coder_eval.orchestrator import Orchestrator
        from coder_eval.orchestrator import settings as orchestrator_settings

        task_dir = tmp_path / "task"
        (task_dir / "reference").mkdir(parents=True)
        (task_dir / "reference" / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        task_file = task_dir / "task.yaml"
        task_file.write_text("# task", encoding="utf-8")

        task = _reference_task(reference_dir="reference")
        orchestrator = Orchestrator(task=task, run_dir=tmp_path / "run", variant_id="v", task_file=task_file)
        orchestrator.result = EvaluationResult(
            task_id=task.task_id,
            task_description=task.description,
            variant_id="v",
            agent_type=task.agent.type,
            started_at=0.0,
            final_status=FinalStatus.SUCCESS,
            iteration_count=0,
            environment_info={},
        )

        # Stop _setup after the sandbox is built: creating a real agent needs creds.
        # Agent construction may still fail; the assertions below only depend on
        # the staging step, which runs first.
        with (
            patch.object(Orchestrator, "_create_and_start_agent", new=AsyncMock(return_value=None), create=True),
            patch.object(type(orchestrator_settings), "validate_api_keys", return_value=None),
            contextlib.suppress(Exception),
        ):
            await orchestrator._setup()

        assert orchestrator._reference_dir is not None, "_setup did not stage the reference"
        assert (orchestrator._reference_dir / "solution.py").read_text(encoding="utf-8") == "SOLUTION=42"
        assert orchestrator.sandbox is not None
        assert orchestrator.sandbox.reference_dir == orchestrator._reference_dir
        await orchestrator._cleanup()

    async def test_cleanup_removes_the_staging_root_even_when_left_at_mode_000(self, tmp_path):
        """A killed run can leave the staged copy unreadable; plain
        rmtree(ignore_errors=True) then silently declines, orphaning a tempdir
        that holds the solution."""
        from coder_eval.orchestrator import _rmtree_restrictive

        root = tmp_path / "staging"
        (root / "reference").mkdir(parents=True)
        (root / "reference" / "solution.py").write_text("SOLUTION=42", encoding="utf-8")
        os.chmod(root / "reference", RESTRICTED_MODE)

        _rmtree_restrictive(root)

        assert not root.exists()
