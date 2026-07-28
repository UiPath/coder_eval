"""Tests for SuccessChecker.check_all_async (GH #55).

BaseCriterion's primary surface is async: a checker overrides exactly one of
``_check_impl`` (sync CPU/file-bound) or ``_check_impl_async`` (genuine async
I/O — llm_judge / agent_judge), and the base derives the other. A checker
counts as "native async" for dispatch purposes iff it overrides
``_check_impl_async`` itself.

``check_all_async`` must:
  - run all native-async criteria concurrently with each other (not serially),
  - run the remaining sync criteria together in a single ``to_thread`` slot,
  - overlap the sync batch with the async batch,
  - preserve result ordering regardless of which path each criterion took,
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
    async def test_async_batch_overlaps_sync_batch(self, checker):
        """The native-async batch and the sync-criteria to_thread batch must overlap."""
        async_fake = _SleepyAsyncChecker()
        sync_fake = _SleepyThreadChecker()
        checker._checker_instances["llm_judge"] = async_fake
        checker._checker_instances["file_exists"] = sync_fake

        criteria = [_llm_criterion("judge"), _file_criterion("file")]
        start = time.monotonic()
        results = await checker.check_all_async(criteria)
        elapsed = time.monotonic() - start

        assert elapsed < SLEEP_SECONDS * 1.5, f"sync and async batches serialized: took {elapsed:.3f}s"
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

    def test_register_criterion_rejects_a_checker_overriding_neither(self):
        from coder_eval.criteria.base import register_criterion
        from coder_eval.models import FileExistsCriterion

        class _NeitherChecker(BaseCriterion[FileExistsCriterion]):
            criterion_type = "neither_test"

        with pytest.raises(TypeError, match="must override _check_impl"):
            register_criterion(_NeitherChecker)
