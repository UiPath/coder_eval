"""Tests for SuccessChecker.check_all_async (GH #55).

BaseCriterion's primary surface is async: a checker overrides exactly one of
``_check_impl`` (sync CPU/file-bound) or ``_check_impl_async`` (genuine async
I/O — llm_judge / agent_judge), and the base derives the other. A checker
counts as "native async" for dispatch purposes iff it overrides
``_check_impl_async`` itself.

``check_all_async`` must:
  - run all native-async criteria concurrently with each other (not serially),
  - run the remaining sync criteria together in a single ``to_thread`` slot,
  - run that sync batch to completion BEFORE starting the native-async batch
    (not concurrently) — several sync criteria mutate the sandbox
    (run_command, uipath_eval) while judge criteria read it, so overlapping
    the two would make scores depend on unrelated timing,
  - preserve result ordering regardless of which path each criterion took,
  - let every native-async sibling settle before re-raising a
    JudgeInfrastructureError (no orphaned judge work),
  - preserve the sync path's error-handling contract (KeyError / generic
    Exception captured into a failed CriterionResult; JudgeInfrastructureError
    escalates).
"""

import asyncio
import time

import pytest

from coder_eval.criteria.base import BaseCriterion
from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import CriterionResult, FileExistsCriterion, LLMJudgeCriterion, SandboxConfig
from coder_eval.sandbox import Sandbox


SLEEP_SECONDS = 0.2


@pytest.fixture
def sandbox(tmp_path):
    config = SandboxConfig(driver="tempdir")
    sb = Sandbox(config, task_id="check_all_async_test")
    try:
        sb.setup()
        yield sb
    finally:
        sb.cleanup()


@pytest.fixture
def checker(sandbox):
    return SuccessChecker(sandbox, init_registry=True, validate_registry=False)


class _SleepyAsyncChecker(BaseCriterion[LLMJudgeCriterion]):
    """Stand-in for llm_judge/agent_judge: overrides _check_impl_async only,
    and sleeps on the event loop (not a thread) — the "native async" shape."""

    criterion_type = "llm_judge"

    def __init__(self, sleep_seconds: float = SLEEP_SECONDS):
        self.sleep_seconds = sleep_seconds
        self.calls = 0

    async def _check_impl_async(self, criterion, sandbox, reference_code=None, *, turn_records=None, context=None):
        self.calls += 1
        await asyncio.sleep(self.sleep_seconds)
        return CriterionResult(criterion_type=self.criterion_type, description=criterion.description, score=1.0)


class _SleepyThreadChecker(BaseCriterion[FileExistsCriterion]):
    """Stand-in for a CPU/file-bound checker: overrides _check_impl only (plain
    sync code) and blocks a thread, not async-native."""

    criterion_type = "file_exists"

    def __init__(self, sleep_seconds: float = SLEEP_SECONDS):
        self.sleep_seconds = sleep_seconds

    def _check_impl(self, criterion, sandbox, reference_code=None, *, turn_records=None, context=None):
        time.sleep(self.sleep_seconds)
        return CriterionResult(criterion_type=self.criterion_type, description=criterion.description, score=1.0)


class _RaisingAsyncChecker(BaseCriterion[LLMJudgeCriterion]):
    criterion_type = "llm_judge"

    def __init__(self, exc: Exception):
        self.exc = exc

    async def _check_impl_async(self, criterion, sandbox, reference_code=None, *, turn_records=None, context=None):
        raise self.exc


def _llm_criterion(description: str) -> LLMJudgeCriterion:
    return LLMJudgeCriterion(description=description, prompt="grade it")


def _file_criterion(description: str, path: str = "x.txt") -> FileExistsCriterion:
    return FileExistsCriterion(description=description, path=path)


class TestNativeAsyncConcurrency:
    @pytest.mark.asyncio
    async def test_two_judge_criteria_run_concurrently(self, checker):
        """Two llm_judge-type criteria must overlap, not serialize (issue #55 point 1)."""
        fake = _SleepyAsyncChecker()
        checker._checker_instances["llm_judge"] = fake

        criteria = [_llm_criterion("a"), _llm_criterion("b")]
        start = time.monotonic()
        results = await checker.check_all_async(criteria)
        elapsed = time.monotonic() - start

        assert fake.calls == 2
        assert elapsed < SLEEP_SECONDS * 1.5, f"judge criteria serialized: took {elapsed:.3f}s"
        assert [r.score for r in results] == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_sync_batch_completes_before_async_batch_starts(self, checker):
        """The sync-criteria to_thread batch must run to completion BEFORE the
        native-async batch starts (not concurrently with it) — several
        first-party sync criteria mutate the sandbox (run_command,
        uipath_eval) while judge criteria read it, so overlapping the two
        would make a judge's score depend on how far a concurrent
        sandbox-mutating command happened to get."""
        async_fake = _SleepyAsyncChecker()
        sync_fake = _SleepyThreadChecker()
        checker._checker_instances["llm_judge"] = async_fake
        checker._checker_instances["file_exists"] = sync_fake

        criteria = [_llm_criterion("judge"), _file_criterion("file")]
        start = time.monotonic()
        results = await checker.check_all_async(criteria)
        elapsed = time.monotonic() - start

        assert elapsed >= SLEEP_SECONDS * 1.8, f"sync and async batches overlapped: took {elapsed:.3f}s"
        assert [r.criterion_type for r in results] == ["llm_judge", "file_exists"]
        assert [r.score for r in results] == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_result_order_preserved_regardless_of_dispatch_path(self, checker):
        """Interleaved sync/async criteria must come back in declaration order."""
        checker._checker_instances["llm_judge"] = _SleepyAsyncChecker(sleep_seconds=0.01)
        checker._checker_instances["file_exists"] = _SleepyThreadChecker(sleep_seconds=0.01)

        criteria = [
            _file_criterion("f1"),
            _llm_criterion("j1"),
            _file_criterion("f2"),
            _llm_criterion("j2"),
        ]
        results = await checker.check_all_async(criteria)
        assert [r.description for r in results] == ["f1", "j1", "f2", "j2"]
        assert [r.criterion_type for r in results] == ["file_exists", "llm_judge", "file_exists", "llm_judge"]


class TestNativeAsyncErrorHandling:
    @pytest.mark.asyncio
    async def test_generic_exception_captured_as_failed_result(self, checker):
        checker._checker_instances["llm_judge"] = _RaisingAsyncChecker(ValueError("boom"))
        results = await checker.check_all_async([_llm_criterion("j")])
        assert len(results) == 1
        assert results[0].score == 0.0
        assert "ValueError: boom" in (results[0].error or "")

    @pytest.mark.asyncio
    async def test_judge_infrastructure_error_propagates(self, checker):
        checker._checker_instances["llm_judge"] = _RaisingAsyncChecker(JudgeInfrastructureError("down"))
        with pytest.raises(JudgeInfrastructureError, match="down"):
            await checker.check_all_async([_llm_criterion("j")])

    @pytest.mark.asyncio
    async def test_judge_infrastructure_error_does_not_orphan_sibling_judges(self, checker):
        """A raising judge must not abandon its siblings mid-flight — the
        gather uses return_exceptions=True so every sibling settles (and any
        side effect it performs completes) before the error is re-raised."""
        completed: list[str] = []

        class _SideEffectAsyncChecker(_SleepyAsyncChecker):
            async def _check_impl_async(
                self, criterion, sandbox, reference_code=None, *, turn_records=None, context=None
            ):
                result = await super()._check_impl_async(
                    criterion, sandbox, reference_code, turn_records=turn_records, context=context
                )
                completed.append(criterion.description)
                return result

        checker._checker_instances["llm_judge"] = _RaisingAsyncChecker(JudgeInfrastructureError("down"))
        checker._checker_instances["agent_judge"] = _SideEffectAsyncChecker(sleep_seconds=SLEEP_SECONDS)

        from coder_eval.models import AgentJudgeCriterion

        criteria = [_llm_criterion("j"), AgentJudgeCriterion(description="sibling", prompt="grade it")]
        with pytest.raises(JudgeInfrastructureError, match="down"):
            await checker.check_all_async(criteria)
        assert completed == ["sibling"], "sibling judge was orphaned instead of being allowed to settle"

    @pytest.mark.asyncio
    async def test_empty_criteria_returns_empty(self, checker):
        assert await checker.check_all_async([]) == []


class TestCheckAllAsyncMatchesSyncBehavior:
    @pytest.mark.asyncio
    async def test_all_sync_criteria_still_produce_same_results_as_check_all(self, checker):
        criteria = [_file_criterion("f1", path="missing.txt")]
        sync_results = checker.check_all(criteria)
        async_results = await checker.check_all_async(criteria)
        assert [r.score for r in sync_results] == [r.score for r in async_results]


class TestNativeAsyncDetection:
    def test_default_checker_is_not_native_async(self, checker):
        """A checker that only overrides _check_impl is dispatched via the sync batch."""
        checker._checker_instances["file_exists"] = _SleepyThreadChecker(sleep_seconds=0.0)
        assert checker._is_native_async("file_exists") is False

    def test_async_override_is_native_async(self, checker):
        """A checker that overrides _check_impl_async is dispatched directly on the loop."""
        checker._checker_instances["llm_judge"] = _SleepyAsyncChecker(sleep_seconds=0.0)
        assert checker._is_native_async("llm_judge") is True

    def test_unregistered_type_is_not_native_async(self, checker):
        assert checker._is_native_async("does_not_exist") is False

    def test_registered_llm_judge_and_agent_judge_are_native_async(self, checker):
        """Pin native-async dispatch for the *real* registered checkers — the
        one-line assertion that encodes the whole point of this module: if a
        future refactor moves llm_judge/agent_judge back to overriding only
        _check_impl, they would silently serialize behind one to_thread slot
        again, and only this test would catch it."""
        assert checker._is_native_async("llm_judge") is True
        assert checker._is_native_async("agent_judge") is True


class TestBaseCriterionSyncAsyncDerivation:
    """Direct unit coverage of BaseCriterion's cross-derivation, bypassing SuccessChecker."""

    def test_sync_only_checker_gets_async_for_free(self):
        checker = _SleepyThreadChecker(sleep_seconds=0.0)
        result = asyncio.run(checker.check_async(_file_criterion("f"), sandbox=None))
        assert result.score == 1.0

    def test_async_only_checker_gets_sync_for_free(self):
        checker = _SleepyAsyncChecker(sleep_seconds=0.0)
        result = checker.check(_llm_criterion("j"), sandbox=None)
        assert result.score == 1.0

    def test_defining_a_checker_overriding_neither_raises_at_class_definition(self):
        """The override contract is enforced by __init_subclass__ at
        class-definition time — before register_criterion (or any other entry
        point, e.g. CriterionRegistry.register called directly) ever runs."""
        from coder_eval.models import FileExistsCriterion

        with pytest.raises(TypeError, match="must override _check_impl"):

            class _NeitherChecker(BaseCriterion[FileExistsCriterion]):
                criterion_type = "neither_test"
