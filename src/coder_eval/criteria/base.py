"""Base criterion checker interface with error handling."""

import asyncio
import logging
import os
import statistics
import traceback
from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar, Concatenate, Literal, ParamSpec, final

from coder_eval.errors import CheckerMisuseError, JudgeInfrastructureError
from coder_eval.models import BaseSuccessCriterion, CriterionAggregate, CriterionResult


if TYPE_CHECKING:
    from pathlib import Path

    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# A criterion's verdict from a PARTIAL, mid-run trajectory (early-stop observability).
# "undecided" means the outcome is not yet knowable from the events seen so far.
LiveVerdict = Literal["pass", "fail", "undecided"]


@dataclass(frozen=True)
class CheckContext:
    """Live run context forwarded to every criterion checker's ``_check_impl``.

    Bundles the pieces of orchestrator state that judge-style criteria
    (``llm_judge`` / ``agent_judge``) need to route their own LLM calls. Carries
    a live object (the resolved ``route``), so it is NOT a ``coder_eval.models``
    Pydantic model — it never gets serialized into a result record.

    Non-judge checkers receive it too (uniform ``_check_impl`` signature) and
    ignore it.
    """

    route: "ApiRoute | None" = None
    reference_dir: "Path | None" = None


# Module-level ParamSpec (rather than the PEP 695 `def f[**P](...)` form ruff's
# UP047 prefers) — CodeQL's Python extractor doesn't yet parse PEP 695 type
# parameters referenced via `P.args`/`P.kwargs` and flags `P` as a potentially
# uninitialized local; a plain `typing.ParamSpec` is unambiguous to both tools.
P = ParamSpec("P")

# Exceptions that must escalate rather than be captured into a scored-0.0
# CriterionResult — a judge-infra outage or a checker-contract misuse is not an
# agent failure. Shared by both handle_criterion_errors(_async) wrappers below.
_ESCALATING_EXCEPTIONS: tuple[type[Exception], ...] = (JudgeInfrastructureError, CheckerMisuseError)


def _failed_result(owner: Any, criterion: BaseSuccessCriterion, exc: Exception, method: str) -> CriterionResult:
    """Build the failed ``CriterionResult`` for a captured (non-escalating)
    checker exception, and log it. Shared by the sync/async
    ``handle_criterion_errors(_async)`` wrapper tails so the two decorators
    differ only in ``def``/``async def`` and ``return``/``return await``.
    """
    exc_info = f"{exc.__class__.__name__}: {exc}"
    tb = ""
    if os.getenv("CODER_EVAL_DEBUG") == "1":
        tb = "\n" + "".join(traceback.format_exc(limit=5))

    criterion_type = criterion.type
    logger.error(
        f"Error in {owner.__class__.__name__}.{method}() for criterion type '{criterion_type}': {exc_info}",
        exc_info=True,  # Adds full stack trace to logs
    )
    return CriterionResult(
        criterion_type=criterion_type,
        description=criterion.description,
        score=0.0,
        details=f"Error during check: {exc_info}{tb}",
        error=exc_info,  # Include exception type and message
    )


def handle_criterion_errors(  # noqa: UP047
    func: Callable[Concatenate[Any, BaseSuccessCriterion, P], CriterionResult],
) -> Callable[Concatenate[Any, BaseSuccessCriterion, P], CriterionResult]:
    """Decorator to handle errors in criterion checkers.

    Wraps checker methods to catch exceptions and return a failed
    CriterionResult with error details instead of raising.

    This is the CENTRALIZED error handling that was in evaluator.py.

    Typed with ``ParamSpec``/``Concatenate`` rather than ``Callable[..., ...]``
    so the decorated method's parameter list (sandbox, reference_code,
    turn_records, context) stays visible to callers instead of erasing to
    ``(...) -> CriterionResult``.
    """

    @wraps(func)
    def wrapper(
        self: Any,
        criterion: BaseSuccessCriterion,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> CriterionResult:
        try:
            return func(self, criterion, *args, **kwargs)
        except _ESCALATING_EXCEPTIONS:
            # Judge infra failure / checker-contract misuse is NOT an agent
            # failure — do not score it 0.0. Propagates to Orchestrator.run()'s
            # broad except → FinalStatus.ERROR.
            raise
        except Exception as e:
            return _failed_result(self, criterion, e, "check")

    return wrapper


def handle_criterion_errors_async(  # noqa: UP047
    func: Callable[Concatenate[Any, BaseSuccessCriterion, P], Awaitable[CriterionResult]],
) -> Callable[Concatenate[Any, BaseSuccessCriterion, P], Awaitable[CriterionResult]]:
    """Async twin of :func:`handle_criterion_errors`, for ``check_async``.

    Same contract: infra failures escalate, everything else is captured into a
    failed ``CriterionResult`` instead of propagating. Same ``ParamSpec``/
    ``Concatenate`` typing rationale applies (see the sync twin's docstring).
    """

    @wraps(func)
    async def wrapper(
        self: Any,
        criterion: BaseSuccessCriterion,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> CriterionResult:
        try:
            return await func(self, criterion, *args, **kwargs)
        except _ESCALATING_EXCEPTIONS:
            raise
        except Exception as e:
            return _failed_result(self, criterion, e, "check_async")

    return wrapper


class BaseCriterion[C: BaseSuccessCriterion](ABC):
    """Abstract base class for all criterion checkers.

    The checking logic's PRIMARY surface is async (``_check_impl_async``) —
    every criterion, at bottom, is "read some inputs, produce a score," and
    async is the strictly more general shape: it covers both a criterion that
    never awaits anything (a CPU/file-bound check) and one that awaits genuine
    I/O (an LLM judge call). A checker implements exactly ONE of the two
    ``_check_impl*`` methods — whichever is its natural form — and the base
    class derives the other automatically:

    - CPU/file-bound criteria (file_exists, command_executed, ...) override
      ``_check_impl`` (plain sync code, no event loop to think about). The
      base's default ``_check_impl_async`` offloads it to a worker thread via
      ``asyncio.to_thread`` so it never blocks the event loop.
    - Criteria that make genuine async I/O (llm_judge, agent_judge) override
      ONLY ``_check_impl_async`` (using an async HTTP client / subprocess
      bridge) — there is no reason to hand-maintain a second, sync-client
      implementation just for the rarely-used direct-sync-call path. The
      base's default ``_check_impl`` derives a sync call by running the async
      one to completion on a fresh event loop (``asyncio.run``).

    ``__init_subclass__`` enforces that a checker overrides EXACTLY ONE of
    the two, at class-definition time — overriding neither would recurse
    forever between the defaults (``asyncio.run`` <-> ``asyncio.to_thread``)
    the first time either is called, and overriding both would let the two
    implementations silently drift into different scores depending on which
    entry point (``check`` vs ``check_async``) ran.

    ``check()`` / ``check_async()`` are FINAL — they apply centralized error
    handling and must not be overridden; implement ``_check_impl`` /
    ``_check_impl_async`` instead.

    Type parameter C binds the checker to its specific criterion model for
    better IDE support and static type checking.

    Example:
        @register_criterion
        class FileExistsChecker(BaseCriterion[FileExistsCriterion]):
            criterion_type = "file_exists"

            def _check_impl(
                self,
                criterion: FileExistsCriterion,
                sandbox: Sandbox,
                reference_code: str | None = None,
            ) -> CriterionResult:
                # Implementation here
                pass
    """

    # Subclasses MUST define this as a class variable
    criterion_type: ClassVar[str]

    # Which polarities this criterion can decide from a PARTIAL, mid-run trajectory.
    # Empty (base default) = not observable mid-run, so it can never arm early-stop.
    # A subclass that reads only turn_records and can decide mid-run declares the
    # polarities it supports (e.g. frozenset({"pass", "fail"})) AND overrides
    # live_verdict; CE025 enforces that the two stay consistent.
    live_stop_polarities: ClassVar[frozenset[str]] = frozenset()

    def __new__(cls, *args: Any, **kwargs: Any) -> "BaseCriterion[C]":
        """Block direct instantiation of ``BaseCriterion`` itself.

        ``__init_subclass__`` below only runs for SUBCLASSES, so with no
        ``@abstractmethod`` left on this class (both ``_check_impl*`` methods
        have concrete default bodies, by design — that's what lets each derive
        the other), plain ``ABCMeta`` no longer blocks ``BaseCriterion()``
        directly. This restores that guarantee without reintroducing an
        abstract method that would break the "override at least one" contract.
        """
        if cls is BaseCriterion:
            raise TypeError("BaseCriterion is abstract and cannot be instantiated directly")
        return super().__new__(cls)

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs: Any) -> None:
        """Enforce the ``_check_impl`` / ``_check_impl_async`` override contract
        at class-definition time (module import), regardless of which entry
        point later registers the class — closing the gap where a subclass
        registered via ``CriterionRegistry.register`` directly (bypassing the
        ``register_criterion`` decorator) escaped the check, and turning the
        mutual-recursion failure mode (``asyncio.run`` <-> ``asyncio.to_thread``
        exhausting OS threads) into an immediate, clearly-named ``TypeError``.

        Enforces "exactly one", not just "at least one": overriding BOTH is
        also rejected — a checker with two live implementations (sync-path
        `_check_impl` and async-path `_check_impl_async`) is free to have them
        drift into different scores for identical agent output depending on
        which entry point (``check`` vs ``check_async``) happened to run it,
        which is exactly the class of bug this derivation design exists to
        eliminate.

        Pass ``abstract=True`` on a class that intentionally implements
        neither (e.g. a shared abstract base for a family of related
        checkers) to opt out of the check for that one class; every one of
        ITS subclasses is still checked normally.
        """
        super().__init_subclass__(**kwargs)
        if abstract:
            return
        overrides_sync = cls._check_impl is not BaseCriterion._check_impl
        overrides_async = cls._check_impl_async is not BaseCriterion._check_impl_async
        if not overrides_sync and not overrides_async:
            raise TypeError(f"{cls.__name__} must override _check_impl or _check_impl_async")
        if overrides_sync and overrides_async:
            msg = f"{cls.__name__} must override exactly one of _check_impl / _check_impl_async, not both"
            raise TypeError(msg)

    @classmethod
    def is_native_async(cls) -> bool:
        """Whether this checker class makes genuine async I/O — i.e. overrides
        ``_check_impl_async`` itself rather than inheriting the base's
        to-thread-wrapped-sync default.

        Public + typed so dispatch code (``SuccessChecker._is_native_async``)
        and anything else that needs to classify a checker doesn't have to
        reach into ``_check_impl_async`` (a "protected" name) on another
        class from a different package.
        """
        return cls._check_impl_async is not BaseCriterion._check_impl_async

    @final
    @handle_criterion_errors
    def check(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        """Execute the criterion check with centralized error handling.

        This method is FINAL - subclasses must NOT override it.
        Implement _check_impl() (or _check_impl_async()) instead.

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison
            turn_records: Optional list of turn records for command inspection
            context: Optional :class:`CheckContext` carrying the live run state
                (``route`` / ``reference_dir``) that judge criteria
                (``agent_judge``, ``llm_judge``) consume. Non-judge checkers
                accept the uniform signature and ignore it.

        Returns:
            CriterionResult with score (0.0-1.0), details, and error info
        """
        return self._check_impl(
            criterion,
            sandbox,
            reference_code,
            turn_records=turn_records,
            context=context,
        )

    def _check_impl(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        """Sync checking logic. Override this OR ``_check_impl_async`` (not both).

        Base default: runs ``_check_impl_async`` to completion on a fresh event
        loop (``asyncio.run``) — the bridge for checkers whose natural form is
        async (they override ``_check_impl_async`` only). Override THIS instead
        when the checker's natural form is plain sync CPU/file-bound code.

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison
            turn_records: Optional list of turn records for command inspection
            context: Optional :class:`CheckContext` (route / reference_dir).
                Consumed by ``llm_judge`` / ``agent_judge``; ignored by
                the rest.

        Returns:
            CriterionResult with score (0.0-1.0), details, and error info

        Raises:
            CheckerMisuseError: this bridge is called from inside a running
                event loop (``asyncio.run`` cannot start a nested loop) — this
                is a caller mistake (the async-primary surface should have
                been awaited instead), not an agent failure, so it escalates
                rather than silently scoring 0.0.
            Any other exception - will be caught by @handle_criterion_errors
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            msg = (
                f"{type(self).__name__} implements only _check_impl_async; "
                f"call check_async()/check_all_async() from an event loop, not check()/check_all()"
            )
            raise CheckerMisuseError(msg)
        return asyncio.run(
            self._check_impl_async(
                criterion,
                sandbox,
                reference_code,
                turn_records=turn_records,
                context=context,
            )
        )

    @final
    @handle_criterion_errors_async
    async def check_async(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        """Async twin of ``check()``. This method is FINAL - subclasses must NOT
        override it. Implement ``_check_impl`` / ``_check_impl_async`` instead.
        """
        return await self._check_impl_async(
            criterion,
            sandbox,
            reference_code,
            turn_records=turn_records,
            context=context,
        )

    async def _check_impl_async(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        """Async checking logic — the PRIMARY implementation surface.

        Base default: offloads ``_check_impl`` to a worker thread via
        ``asyncio.to_thread`` so a CPU/file-bound sync checker never blocks the
        event loop. Override THIS instead when the checker makes genuine async
        I/O (``llm_judge``, ``agent_judge``) — an async HTTP client / subprocess
        bridge that actually yields control while waiting. Leave ``_check_impl``
        unimplemented in that case: the base default derives a sync call from
        this one via ``asyncio.run()``, so no second (sync-client) implementation
        is needed.
        """
        return await asyncio.to_thread(
            self._check_impl,
            criterion,
            sandbox,
            reference_code,
            turn_records=turn_records,
            context=context,
        )

    def live_verdict(
        self,
        criterion: C,
        turn_records: list["TurnRecord"],
    ) -> LiveVerdict:
        """Decide this criterion from a PARTIAL, mid-run trajectory (early-stop).

        Reads ONLY ``turn_records`` — a live verdict, by definition, may not peek
        at the finished sandbox (that would invite end-state peeking), so there is
        no ``sandbox`` parameter. Returns ``"pass"``/``"fail"`` only when the
        outcome is already knowable from the events seen so far, else
        ``"undecided"``.

        This only *triggers* an early stop; the authoritative scores always come
        from ``check()``/``_check_impl`` run on the frozen trajectory after the
        stop, so a live/final divergence can never corrupt scoring.

        Base default: ``"undecided"`` (not observable mid-run). Subclasses that
        override this MUST also declare a non-empty ``live_stop_polarities`` (and
        vice versa) — enforced by lint rule CE025.
        """
        return "undecided"

    @classmethod
    def live_decidable_polarities(cls, criterion: C) -> frozenset[str]:
        """Which polarities THIS criterion *instance* can actually decide mid-run.

        ``live_stop_polarities`` is a class-level *capability* — the widest set
        of polarities the checker's ``live_verdict`` could ever emit. But for
        some criteria whether a given polarity can fire depends on the instance's
        configuration, not just its type. ``command_executed`` is the canonical
        case: it can live-``pass`` only with no upper bound, and live-``fail``
        only with one, so a specific criterion may support strictly fewer
        polarities than its class advertises (down to none — a "dead arm").

        ``validate_early_stop`` gates the requested ``stop_when`` polarity on THIS
        set, not the ClassVar, so an instance that can never decide its armed
        polarity is rejected at resolution rather than silently degrading to a
        full run (the "never a silent no-op" guarantee).

        Default: the class-level ``live_stop_polarities`` — correct for every
        criterion whose decidability is purely type-level (e.g. ``skill_triggered``).
        Overrides MUST return a subset of ``live_stop_polarities`` (a criterion
        cannot decide a polarity its ``live_verdict`` never emits).
        """
        return cls.live_stop_polarities

    def aggregate(
        self,
        criterion: C,
        per_row_results: list[CriterionResult],
    ) -> CriterionAggregate | None:
        """Across-row aggregate for dataset-backed tasks.

        Default implementation emits summary statistics over the per-row scores:
        ``count``, ``mean``, ``median``, ``std``, ``min``, ``max``. This gives
        every criterion a thresholdable baseline for free (e.g. a ``file_exists``
        task can gate on ``suite_thresholds: {mean: 0.9}``).

        Returns ``None`` only when there are no per-row results (empty dataset).

        Subclasses with richer signals (classification, per-label metrics, etc.)
        should override and call ``super().aggregate(...)`` to inherit these
        baseline stats, then merge their own metrics and details on top.
        """
        if not per_row_results:
            return None

        scores = [r.score for r in per_row_results]
        std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        metrics: dict[str, float] = {
            "count": float(len(scores)),
            "mean": statistics.fmean(scores),
            "median": statistics.median(scores),
            "std": std,
            "min": min(scores),
            "max": max(scores),
        }
        return CriterionAggregate(
            criterion_type=criterion.type,
            metrics=metrics,
            threshold_checks=[],
            passed=True,
            details={},
        )


# Decorator for registration (defined here to avoid circular imports)
def register_criterion(cls: type[BaseCriterion[Any]]) -> type[BaseCriterion[Any]]:
    """Decorator to register a criterion checker.

    Moved here from __init__.py to prevent circular import issues.

    The ``_check_impl`` / ``_check_impl_async`` override contract is enforced
    by ``BaseCriterion.__init_subclass__`` at class-definition time (before
    this decorator ever runs), so it applies uniformly regardless of which
    entry point registers the class — this decorator, or
    ``CriterionRegistry.register`` called directly.

    Usage:
        @register_criterion
        class MyChecker(BaseCriterion[MyCriterion]):
            criterion_type = "my_type"
            ...
    """
    from coder_eval.criteria import CriterionRegistry

    return CriterionRegistry.register(cls)
