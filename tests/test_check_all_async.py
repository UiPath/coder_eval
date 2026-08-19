"""Tests for SuccessChecker.check_all_async.

BaseCriterion's primary surface is async: a checker overrides exactly one of
``_check_impl`` (sync CPU/file-bound) or ``_check_impl_async`` (genuine async
I/O — llm_judge / agent_judge), and the base derives the other. A checker
counts as "native async" for dispatch purposes iff it overrides
``_check_impl_async`` itself.

``check_all_async`` currently runs every criterion SEQUENTIALLY, strictly in
declaration order — the same order/isolation guarantee ``check_all`` (the
sync twin) provides. Concurrent dispatch of adjacent judge criteria (the
GH #55 motivation) is intentionally deferred to a follow-up PR; this module
pins the sequential contract in the meantime:
  - every criterion is fully awaited before the next one starts, regardless
    of whether it's native-async or sync-offloaded-to-a-thread,
  - declaration order is preserved exactly (matches ``check_all``),
  - the sync path's error-handling contract carries over unchanged (KeyError /
    generic Exception captured into a failed CriterionResult;
    JudgeInfrastructureError escalates and stops any remaining criteria).
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.criteria.base import BaseCriterion
from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import (
    CriterionResult,
    FileExistsCriterion,
    LLMJudgeCriterion,
    RunCommandCriterion,
    SandboxConfig,
)
from coder_eval.models.routing import DirectRoute
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
    and sleeps on the event loop (not a thread) — the "native async" shape.

    Optionally appends markers to a shared ``events`` list (for ordering
    assertions) instead of relying on wall-clock timing.
    """

    criterion_type = "llm_judge"

    def __init__(
        self,
        sleep_seconds: float = SLEEP_SECONDS,
        events: list[str] | None = None,
        label: str = "async",
    ):
        self.sleep_seconds = sleep_seconds
        self.calls = 0
        self.events = events
        self.label = label

    async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
        self.calls += 1
        if self.events is not None:
            self.events.append(f"{self.label}-start")
        await asyncio.sleep(self.sleep_seconds)
        if self.events is not None:
            self.events.append(f"{self.label}-end")
        return CriterionResult(criterion_type=self.criterion_type, description=criterion.description, score=1.0)


class _SleepyThreadChecker(BaseCriterion[FileExistsCriterion]):
    """Stand-in for a CPU/file-bound checker: overrides _check_impl only (plain
    sync code) and blocks a thread, not async-native."""

    criterion_type = "file_exists"

    def __init__(self, sleep_seconds: float = SLEEP_SECONDS, events: list[str] | None = None, label: str = "sync"):
        self.sleep_seconds = sleep_seconds
        self.events = events
        self.label = label

    def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
        if self.events is not None:
            self.events.append(f"{self.label}-start")
        time.sleep(self.sleep_seconds)
        if self.events is not None:
            self.events.append(f"{self.label}-end")
        return CriterionResult(criterion_type=self.criterion_type, description=criterion.description, score=1.0)


class _RaisingAsyncChecker(BaseCriterion[LLMJudgeCriterion]):
    criterion_type = "llm_judge"

    def __init__(self, exc: Exception):
        self.exc = exc

    async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
        raise self.exc


def _llm_criterion(description: str) -> LLMJudgeCriterion:
    return LLMJudgeCriterion(description=description, prompt="grade it")


def _file_criterion(description: str, path: str = "x.txt") -> FileExistsCriterion:
    return FileExistsCriterion(description=description, path=path)


class TestSequentialExecution:
    @pytest.mark.asyncio
    async def test_two_judge_criteria_run_sequentially_not_concurrently(self, checker):
        """check_all_async currently runs every criterion SEQUENTIALLY —
        concurrent dispatch of adjacent judges (the GH #55 motivation) is
        deferred to a follow-up PR. Two llm_judge-type criteria must NOT
        overlap: the first must fully finish (start AND end) before the
        second starts, proven deterministically via ordered event markers."""
        events: list[str] = []
        fake = _SleepyAsyncChecker(sleep_seconds=0.01, events=events)

        checker._checker_instances["llm_judge"] = fake
        criteria = [_llm_criterion("a"), _llm_criterion("b")]
        results = await asyncio.wait_for(checker.check_all_async(criteria), timeout=5.0)

        assert fake.calls == 2
        assert events == ["async-start", "async-end", "async-start", "async-end"], (
            f"judge criteria overlapped instead of running sequentially: {events}"
        )
        assert [r.score for r in results] == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_sync_and_async_criteria_never_overlap_and_preserve_declaration_order(self, checker):
        """Sync and native-async criteria must never overlap — several
        first-party sync criteria mutate the sandbox (run_command,
        uipath_eval) while judge criteria read it, so overlapping the two
        would make a judge's score depend on how far a concurrent
        sandbox-mutating command happened to get. Declaration order must be
        preserved exactly, matching check_all's serial semantics.

        Proven deterministically via ordered event markers instead of a
        wall-clock margin, for BOTH orderings.
        """
        events: list[str] = []
        async_fake = _SleepyAsyncChecker(sleep_seconds=0.01, events=events, label="async")
        sync_fake = _SleepyThreadChecker(sleep_seconds=0.01, events=events, label="sync")
        checker._checker_instances["llm_judge"] = async_fake
        checker._checker_instances["file_exists"] = sync_fake

        criteria = [_llm_criterion("judge"), _file_criterion("file")]
        results = await asyncio.wait_for(checker.check_all_async(criteria), timeout=5.0)

        assert events == ["async-start", "async-end", "sync-start", "sync-end"], (
            f"declared order [judge, file] was not preserved: {events}"
        )
        assert [r.criterion_type for r in results] == ["llm_judge", "file_exists"]
        assert [r.score for r in results] == [1.0, 1.0]

    async def test_sync_before_async_declaration_order_also_preserved(self, checker):
        """Same guarantee, reversed declaration order — [file, judge]."""
        events: list[str] = []
        async_fake = _SleepyAsyncChecker(sleep_seconds=0.01, events=events, label="async")
        sync_fake = _SleepyThreadChecker(sleep_seconds=0.01, events=events, label="sync")
        checker._checker_instances["llm_judge"] = async_fake
        checker._checker_instances["file_exists"] = sync_fake

        criteria = [_file_criterion("file"), _llm_criterion("judge")]
        results = await asyncio.wait_for(checker.check_all_async(criteria), timeout=5.0)

        assert events == ["sync-start", "sync-end", "async-start", "async-end"], (
            f"declared order [file, judge] was not preserved: {events}"
        )
        assert [r.criterion_type for r in results] == ["file_exists", "llm_judge"]
        assert [r.score for r in results] == [1.0, 1.0]

    @pytest.mark.asyncio
    async def test_run_command_writes_are_visible_to_subsequent_llm_judge(self, tmp_path):
        """End-to-end regression test (real registered run_command + llm_judge
        checkers, not fakes): a sandbox-mutating sync criterion must complete
        BEFORE a sandbox-reading judge criterion declared after it starts, so
        the judge's view of the sandbox is deterministic."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, task_id="ordering_regression_test")
        sandbox.setup()
        try:
            real_checker = SuccessChecker(sandbox, init_registry=True, validate_registry=False, route=DirectRoute())
            run_criterion = RunCommandCriterion(
                description="write file",
                command="sleep 0.2 && printf hello > built.txt",
            )
            judge_criterion = LLMJudgeCriterion(
                description="judge",
                prompt="grade it",
                files=["built.txt"],
                include_agent_output=False,
                include_tool_calls=False,
                include_dialog=False,
            )

            def fake_invoke(**kwargs):
                saw_hello = "hello" in kwargs["user"]
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "submit_verdict",
                            "input": {"score": 1.0 if saw_hello else 0.0, "rationale": "ok", "findings": []},
                        }
                    ]
                }

            with patch(
                "coder_eval.criteria.llm_judge.invoke_anthropic_judge_async",
                new=AsyncMock(side_effect=fake_invoke),
            ):
                results = await real_checker.check_all_async([run_criterion, judge_criterion])

            assert results[0].score == 1.0, "run_command should have succeeded"
            assert results[1].score == 1.0, (
                "llm_judge did not see the file run_command wrote — the sandbox-mutating "
                "sync batch and the sandbox-reading judge batch overlapped"
            )
        finally:
            sandbox.cleanup(preserve=False)

    @pytest.mark.asyncio
    async def test_llm_judge_declared_before_run_command_sees_pre_mutation_state(self, tmp_path):
        """Reversed declaration order — [llm_judge, run_command] — must match
        check_all's strict declaration-order semantics: the judge, declared
        first, must see PRE-mutation state, exactly like it would running
        serially (which is exactly what check_all_async does today)."""
        config = SandboxConfig(driver="tempdir")
        sandbox = Sandbox(config, task_id="reversed_ordering_regression_test")
        sandbox.setup()
        try:
            real_checker = SuccessChecker(sandbox, init_registry=True, validate_registry=False, route=DirectRoute())
            judge_criterion = LLMJudgeCriterion(
                description="judge",
                prompt="grade it",
                files=["built.txt"],
                include_agent_output=False,
                include_tool_calls=False,
                include_dialog=False,
            )
            run_criterion = RunCommandCriterion(
                description="write file",
                command="sleep 0.2 && printf hello > built.txt",
            )

            def fake_invoke(**kwargs):
                saw_hello = "hello" in kwargs["user"]
                return {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "submit_verdict",
                            "input": {"score": 1.0 if saw_hello else 0.0, "rationale": "ok", "findings": []},
                        }
                    ]
                }

            sandbox2 = Sandbox(config, task_id="reversed_ordering_regression_test_sync_twin")
            sandbox2.setup()
            try:
                sync_checker = SuccessChecker(
                    sandbox2, init_registry=True, validate_registry=False, route=DirectRoute()
                )
                with patch(
                    "coder_eval.criteria.llm_judge.invoke_anthropic_judge_async",
                    new=AsyncMock(side_effect=fake_invoke),
                ):
                    async_results = await real_checker.check_all_async([judge_criterion, run_criterion])
                    # check_all is the fully-sync surface: run it off the test's own
                    # event loop (asyncio.to_thread) so its derived asyncio.run bridge
                    # doesn't see a running loop and raise CheckerMisuseError. A
                    # separate sandbox/checker avoids reusing built.txt from the
                    # check_all_async run above, which would let the sync run's
                    # judge see already-mutated state regardless of ordering.
                    sync_results = await asyncio.to_thread(sync_checker.check_all, [judge_criterion, run_criterion])
            finally:
                sandbox2.cleanup(preserve=False)

            assert async_results[1].score == 1.0, "run_command should have succeeded"
            assert async_results[0].score == 0.0, "llm_judge declared BEFORE run_command must see PRE-mutation state"
            assert [r.score for r in async_results] == [r.score for r in sync_results], (
                "check_all_async must agree with check_all's declaration-order semantics"
            )
        finally:
            sandbox.cleanup(preserve=False)

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
    async def test_judge_infrastructure_error_stops_remaining_criteria(self, checker):
        """Sequential dispatch means a JudgeInfrastructureError from one
        criterion propagates immediately, exactly like check_all's serial
        list-building — criteria declared AFTER the raising one never even
        start (there is nothing concurrent to orphan, since nothing runs
        concurrently)."""
        ran: list[str] = []

        class _TrackingAsyncChecker(_SleepyAsyncChecker):
            async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
                ran.append(criterion.description)
                return await super()._check_impl_async(criterion, sandbox, turn_records=turn_records, context=context)

        checker._checker_instances["llm_judge"] = _RaisingAsyncChecker(JudgeInfrastructureError("down"))
        checker._checker_instances["agent_judge"] = _TrackingAsyncChecker(sleep_seconds=0.0)

        from coder_eval.models import AgentJudgeCriterion

        criteria = [_llm_criterion("j"), AgentJudgeCriterion(description="never_runs", prompt="grade it")]
        with pytest.raises(JudgeInfrastructureError, match="down"):
            await checker.check_all_async(criteria)
        assert ran == [], "criterion declared after the raising one should never have started"

    @pytest.mark.asyncio
    async def test_empty_criteria_returns_empty(self, checker):
        assert await checker.check_all_async([]) == []

    @pytest.mark.asyncio
    async def test_native_async_checker_constructor_failure_scores_zero_not_abort(self, checker):
        """_is_native_async's own docstring names this as its design
        rationale — classification reads the registered CLASS precisely so it
        never has to construct the checker, meaning a constructor failure is
        caught by _check_single_async's ordinary error boundary (same as any
        other checker exception) and scores that ONE criterion 0.0, instead of
        escaping check_all_async and aborting the whole task."""
        from coder_eval.criteria import CriterionRegistry

        class _BoomOnInitChecker(BaseCriterion[LLMJudgeCriterion]):
            criterion_type = "boom_on_init_test"

            def __init__(self):
                raise RuntimeError("ctor boom")

            async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
                raise NotImplementedError

        CriterionRegistry.register(_BoomOnInitChecker)
        try:
            criterion = _llm_criterion("j").model_copy(update={"type": "boom_on_init_test"})
            results = await checker.check_all_async([criterion])
        finally:
            CriterionRegistry._checkers.pop("boom_on_init_test", None)

        assert len(results) == 1
        assert results[0].score == 0.0
        assert "ctor boom" in (results[0].error or "")

    def test_checker_misuse_error_escalates_through_success_checker_not_scored_zero(self, checker):
        """CheckerMisuseError must escalate through SuccessChecker.check / check_all
        exactly like JudgeInfrastructureError, not fall into _check_single's generic
        `except Exception` arm and get scored 0.0. The prior fix only pinned this at
        the BaseCriterion.check() layer (test_async_only_checker_sync_bridge_raises_loudly_from_a_running_loop);
        this test exercises it through the SuccessChecker entry point a caller
        actually uses, since _check_single/_check_single_async each have their own
        except clauses that must also name CheckerMisuseError."""
        from coder_eval.errors import CheckerMisuseError

        checker._checker_instances["llm_judge"] = _SleepyAsyncChecker(sleep_seconds=0.0)

        async def call_check_from_running_loop():
            checker.check(_llm_criterion("j"))

        with pytest.raises(CheckerMisuseError, match="check_async"):
            asyncio.run(call_check_from_running_loop())


class TestCheckAllAsyncMatchesSyncBehavior:
    @pytest.mark.asyncio
    async def test_all_sync_criteria_still_produce_same_results_as_check_all(self, checker):
        criteria = [_file_criterion("f1", path="missing.txt")]
        sync_results = checker.check_all(criteria)
        async_results = await checker.check_all_async(criteria)
        assert [r.score for r in sync_results] == [r.score for r in async_results]


class TestNativeAsyncDetection:
    def test_sync_only_checker_registered_class_is_not_native_async(self, checker):
        """_is_native_async classifies the REGISTERED CLASS, not whatever
        instance happens to be cached in _checker_instances — register a
        throwaway sync-only class under a fresh type name (rather than
        injecting into _checker_instances, which _is_native_async never
        reads) so the assertion actually exercises the class-based path."""
        from coder_eval.criteria import CriterionRegistry

        class _ThrowawaySyncChecker(BaseCriterion[FileExistsCriterion]):
            criterion_type = "throwaway_sync_test"

            def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
                raise NotImplementedError

        CriterionRegistry.register(_ThrowawaySyncChecker)
        try:
            assert checker._is_native_async("throwaway_sync_test") is False
        finally:
            CriterionRegistry._checkers.pop("throwaway_sync_test", None)

    def test_async_only_checker_registered_class_is_native_async(self, checker):
        """Same as above, for a throwaway async-only class."""
        from coder_eval.criteria import CriterionRegistry

        class _ThrowawayAsyncChecker(BaseCriterion[LLMJudgeCriterion]):
            criterion_type = "throwaway_async_test"

            async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
                raise NotImplementedError

        CriterionRegistry.register(_ThrowawayAsyncChecker)
        try:
            assert checker._is_native_async("throwaway_async_test") is True
        finally:
            CriterionRegistry._checkers.pop("throwaway_async_test", None)

    def test_checker_instances_injection_does_not_affect_classification(self, checker):
        """Pin the documented class-not-instance rule directly: injecting a
        NATIVE-ASYNC fake under a SYNC type's registered name must not flip
        the classification — dispatch (_get_checker_instance) and
        classification (_is_native_async) intentionally read different
        sources, and this test would catch them silently diverging."""
        checker._checker_instances["file_exists"] = _SleepyAsyncChecker(sleep_seconds=0.0)
        assert checker._is_native_async("file_exists") is False

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

    def test_is_native_async_classmethod_on_checker_class(self):
        """BaseCriterion.is_native_async() is the public, typed capability
        SuccessChecker._is_native_async delegates to."""
        assert _SleepyAsyncChecker.is_native_async() is True
        assert _SleepyThreadChecker.is_native_async() is False


class TestBaseCriterionSyncAsyncDerivation:
    """Direct unit coverage of BaseCriterion's cross-derivation, bypassing SuccessChecker."""

    def test_sync_only_checker_gets_async_for_free(self):
        checker = _SleepyThreadChecker(sleep_seconds=0.0)
        result = asyncio.run(checker.check_async(_file_criterion("f"), sandbox=None))
        assert result.score == 1.0

    def test_sync_only_checker_derived_async_never_blocks_the_loop(self):
        """The contract _check_impl_async's base default promises is "offloads
        _check_impl to a worker thread ... so a CPU/file-bound sync checker
        never blocks the event loop" — assert the thread-affinity property
        itself, not just the score, so a future rewrite that accidentally
        calls _check_impl directly on the loop (same score, lost guarantee)
        would fail this test."""

        class _IdentRecordingChecker(BaseCriterion[FileExistsCriterion]):
            criterion_type = "file_exists"

            def __init__(self):
                self.check_impl_thread_ident: int | None = None

            def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
                self.check_impl_thread_ident = threading.get_ident()
                return CriterionResult(criterion_type=self.criterion_type, description=criterion.description, score=1.0)

        checker = _IdentRecordingChecker()

        async def run() -> int:
            loop_ident = threading.get_ident()
            await checker.check_async(_file_criterion("f"), sandbox=None)
            return loop_ident

        loop_ident = asyncio.run(run())
        assert checker.check_impl_thread_ident is not None
        assert checker.check_impl_thread_ident != loop_ident, "_check_impl ran on the event loop's own thread"

    def test_async_only_checker_gets_sync_for_free(self):
        checker = _SleepyAsyncChecker(sleep_seconds=0.0)
        result = checker.check(_llm_criterion("j"), sandbox=None)
        assert result.score == 1.0

    def test_async_only_checker_sync_bridge_raises_loudly_from_a_running_loop(self):
        """The derived sync `_check_impl` (asyncio.run bridge) must raise a
        loud, named CheckerMisuseError — not silently return a score-0.0
        CriterionResult — when called from inside a running event loop, e.g.
        a library/embedder using the public check()/check_all() sync surface
        from async host code. Without this guard, asyncio.run's RuntimeError
        gets caught by @handle_criterion_errors and turns a real score into a
        misleading 0.0."""
        from coder_eval.errors import CheckerMisuseError

        checker = _SleepyAsyncChecker(sleep_seconds=0.0)

        async def call_from_running_loop():
            checker.check(_llm_criterion("j"), sandbox=None)

        with pytest.raises(CheckerMisuseError, match="check_async"):
            asyncio.run(call_from_running_loop())

    def test_checker_overriding_both_impls_raises_at_class_definition(self):
        """The exactly-ONE contract's other half: overriding BOTH _check_impl
        and _check_impl_async is also rejected — a checker with two live
        implementations could silently drift into different scores for the
        same input depending on which entry point (check vs check_async) ran."""
        import types

        from coder_eval.models import FileExistsCriterion

        def _check_impl(self, criterion, sandbox, *, turn_records=None, context=None):
            raise NotImplementedError

        async def _check_impl_async(self, criterion, sandbox, *, turn_records=None, context=None):
            raise NotImplementedError

        with pytest.raises(TypeError, match="not both"):
            types.new_class(
                "_BothChecker",
                (BaseCriterion[FileExistsCriterion],),
                exec_body=lambda ns: ns.update(
                    criterion_type="both_test", _check_impl=_check_impl, _check_impl_async=_check_impl_async
                ),
            )

    def test_abstract_intermediate_base_opts_out_of_the_override_check(self):
        """A shared abstract base for a family of checkers (implements neither
        _check_impl) can opt out with abstract=True; a concrete subclass of it
        is still checked normally."""
        import types

        from coder_eval.models import FileExistsCriterion

        intermediate = types.new_class(
            "_AbstractFamilyBase",
            (BaseCriterion[FileExistsCriterion],),
            kwds={"abstract": True},
            exec_body=lambda ns: None,
        )

        # The intermediate itself defines neither impl and did not raise.
        assert intermediate._check_impl is BaseCriterion._check_impl

        # A concrete subclass that ALSO overrides neither is still rejected.
        with pytest.raises(TypeError, match="must override _check_impl"):
            types.new_class(
                "_StillNeitherChecker",
                (intermediate,),
                exec_body=lambda ns: ns.update(criterion_type="still_neither_test"),
            )

    def test_base_criterion_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError, match="abstract"):
            BaseCriterion()

    def test_defining_a_checker_overriding_neither_raises_at_class_definition(self):
        """The override contract is enforced by __init_subclass__ at
        class-definition time — before register_criterion (or any other entry
        point, e.g. CriterionRegistry.register called directly) ever runs.

        Built via ``type(...)`` rather than a ``class`` statement so no name is
        bound to the (never fully constructed) class — __init_subclass__ raises
        before the class object exists, so there would be nothing to reference
        afterward anyway.
        """
        import types

        from coder_eval.models import FileExistsCriterion

        with pytest.raises(TypeError, match="must override _check_impl"):
            types.new_class(
                "_NeitherChecker",
                (BaseCriterion[FileExistsCriterion],),
                exec_body=lambda ns: ns.update(criterion_type="neither_test"),
            )
