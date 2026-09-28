"""Success criterion checking with pluggable criterion types.

This module provides the SuccessChecker class which orchestrates
criterion checking using the registered criterion checkers from
the criteria registry.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..criteria import BaseCriterion, CriterionRegistry, init_criteria
from ..criteria.base import _ESCALATING_EXCEPTIONS, CheckContext
from ..models import CriteriaResults, CriterionResult, SuccessCriteria, SuccessCriterion, TurnRecords
from ..sandbox import Sandbox


if TYPE_CHECKING:
    from ..models.routing import ApiRoute


# Get module logger
logger = logging.getLogger(__name__)


def _short_failure_reason(result: CriterionResult, max_len: int = 200) -> str:
    """Return a one-line, length-capped failure reason for a criterion result.

    Prefers ``result.error``; then falls back to the first ``stderr:`` line in
    ``result.details``; then the first non-empty details line. Multi-line text
    is collapsed with ``" | "`` separators before truncation. Returns
    ``"no details"`` when nothing usable is available.

    Shared by ``SuccessChecker`` (console fail log) and
    ``orchestrator._emit_criteria_event`` (streamed ``failure_reason`` field)
    so both surfaces show identical text.
    """

    def _collapse(text: str) -> str:
        lines = [line.strip() for line in text.splitlines()]
        collapsed = " | ".join(line for line in lines if line)
        if len(collapsed) > max_len:
            return collapsed[: max_len - 1] + "…"
        return collapsed

    if result.error:
        return _collapse(result.error) or "no details"
    if result.details:
        for line in result.details.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("stderr:"):
                reason = stripped[len("stderr:") :].strip()
                if reason:
                    return _collapse(reason)
        for line in result.details.splitlines():
            stripped = line.strip()
            if stripped:
                return _collapse(stripped)
    return "no details"


class SuccessChecker:
    """Orchestrates criterion checking using registered checkers."""

    def __init__(
        self,
        sandbox: Sandbox,
        init_registry: bool = True,
        validate_registry: bool = True,
        route: "ApiRoute | None" = None,
    ):
        """Initialize the success checker.

        Args:
            sandbox: Sandbox instance for running checks
            init_registry: Whether to initialize the criteria registry
            validate_registry: Whether to validate all expected types are registered
            route: Resolved ``ApiRoute`` from the orchestrator. Forwarded to every
                checker's ``check()`` so criteria that spawn sub-agents (e.g.
                ``agent_judge``) can route through the same backend (Direct /
                Bedrock) as the main coding agent. ``None`` is acceptable
                for non-sub-agent criteria; ``agent_judge`` requires a route.
                Any ``checker_context.api_route.model`` override is already
                baked into ``route.model`` by ``resolve_evaluation_route``.
        """
        self.sandbox = sandbox
        self._checker_instances: dict[str, BaseCriterion[Any]] = {}
        # The per-run staged copy, set by check()/check_all() and reused by calls
        # that do not pass it explicitly.
        self._reference_dir: Path | None = None
        # Cached turn records - set by check()/check_all() when provided
        self._turn_records: TurnRecords | None = None
        self.route = route
        # Accumulated HERE, not at the four orchestrator call sites, so a fifth
        # cannot be added without it. `None` until something is checked, so an
        # ungraded row reports "never measured" rather than 0.0 (CE058).
        # Rationale: .claude/notes/contracts.md § What escalates instead of scoring 0.0
        self.grading_ms: float | None = None

        # V3: Lazy initialization - registry loaded here, not at import
        if init_registry:
            init_criteria(validate=validate_registry)

    def _resolve_refs(
        self,
        turn_records: TurnRecords | None,
        reference_dir: Path | None,
    ) -> tuple[TurnRecords | None, Path | None]:
        """Persist reference_dir / turn_records for subsequent calls that don't pass
        them explicitly, and resolve the effective values for THIS call. Shared by
        ``check`` / ``check_all`` / ``check_all_async`` so the persist-then-resolve
        preamble lives in exactly one place.
        """
        if reference_dir is not None:
            self._reference_dir = reference_dir
        if turn_records is not None:
            self._turn_records = turn_records
        ref_dir = reference_dir if reference_dir is not None else self._reference_dir
        records = turn_records if turn_records is not None else self._turn_records
        return records, ref_dir

    def check(
        self,
        criterion: SuccessCriterion,
        *,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriterionResult:
        """Check a single criterion (backward compatibility wrapper).

        Args:
            criterion: Criterion definition
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory
                (from ``task.reference.directory``). Consumed by ``reference_comparison`` (which scores 0.0 without
                it), ``llm_judge`` and ``agent_judge``; other criteria accept the
                uniform signature and ignore it.

        Returns:
            CriterionResult with score
        """
        records, ref_dir = self._resolve_refs(turn_records, reference_dir)
        return self._check_single(criterion, records, ref_dir)

    def check_all(
        self,
        criteria: SuccessCriteria,
        *,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriteriaResults:
        """Check all success criteria.

        Args:
            criteria: List of criterion definitions
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory.
                Consumed by ``reference_comparison`` (which scores 0.0 without
                it), ``llm_judge`` and ``agent_judge``; other criteria accept the
                uniform signature and ignore it.

        Returns:
            List of criterion results with scores
        """
        records, ref_dir = self._resolve_refs(turn_records, reference_dir)
        return self._check_all_sync(criteria, records, ref_dir)

    async def check_all_async(
        self,
        criteria: SuccessCriteria,
        *,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriteriaResults:
        """Async twin of ``check_all`` — the orchestrator's entry point.

        Runs every criterion SEQUENTIALLY, strictly in declaration order, with the
        same isolation guarantee ``check_all`` provides. A natively-async checker is
        awaited directly on the event loop; every other one is offloaded to a worker
        thread. Neither is concurrency BETWEEN criteria — each is fully awaited
        before the next starts.

        Args:
            criteria: List of criterion definitions.
            turn_records: Optional turn records for command inspection.
            reference_dir: Optional resolved path to a reference directory.
                Consumed by ``reference_comparison`` (which scores 0.0 without it),
                ``llm_judge`` and ``agent_judge``; the rest ignore it.

        Returns:
            List of criterion results with scores, in the same order as ``criteria``.
        """
        records, ref_dir = self._resolve_refs(turn_records, reference_dir)

        # Monotonic: a wall-clock delta would move if the clock stepped mid-grade,
        # and an `agent_judge` criterion can run for minutes.
        started = time.monotonic()
        results: list[CriterionResult] = []
        try:
            for criterion in criteria:
                if self._is_native_async(criterion.type):
                    results.append(await self._check_single_async(criterion, records, ref_dir))
                else:
                    results.append(await asyncio.to_thread(self._check_single, criterion, records, ref_dir))
        finally:
            # In `finally` so a grade that raises still books what it spent.
            self.grading_ms = (self.grading_ms or 0.0) + (time.monotonic() - started) * 1000.0
        return results

    def _is_native_async(self, criterion_type: str) -> bool:
        """Whether this criterion type's checker makes genuine async I/O.

        Delegates to the checker class's own public ``is_native_async()``
        classmethod (see ``BaseCriterion``) rather than comparing
        ``_check_impl_async`` identity here — that keeps the sync/async
        classification part of the documented, typed plugin contract instead
        of this module reaching into another package's protected member. An
        unregistered type resolves to False so its ``KeyError`` surfaces
        through the shared sync error-handling path in ``_check_single`` /
        ``_check_single_async``.

        Reads the registered CLASS (``CriterionRegistry.get_checker``), not a
        constructed instance — this classification runs in
        ``check_all_async`` before any per-criterion error boundary, so it must
        never invoke a checker's ``__init__`` (a constructor failure would
        otherwise escape ``check_all_async`` entirely instead of scoring that
        one criterion 0, which is the contract ``_check_single`` /
        ``_check_single_async`` provide).
        """
        try:
            checker_class = CriterionRegistry.get_checker(criterion_type)
        except KeyError:
            return False
        return checker_class.is_native_async()

    def _check_all_sync(
        self,
        criteria: SuccessCriteria,
        turn_records: TurnRecords | None,
        reference_dir: Path | None,
    ) -> CriteriaResults:
        return [self._check_single(criterion, turn_records, reference_dir) for criterion in criteria]

    def _get_checker_instance(self, criterion_type: str) -> BaseCriterion[Any]:
        """Get or create a checker instance (V3: cached).

        Args:
            criterion_type: The criterion type

        Returns:
            Checker instance (reused within this evaluation run)
        """
        if criterion_type not in self._checker_instances:
            checker_class = CriterionRegistry.get_checker(criterion_type)
            self._checker_instances[criterion_type] = checker_class()
        return self._checker_instances[criterion_type]

    def _finalize_result(self, criterion: SuccessCriterion, result: CriterionResult) -> CriterionResult:
        """Stamp pass_threshold/gating onto a checker's result and log it. Shared
        by the sync/async success paths in ``_check_single`` / ``_check_single_async``
        so the two only differ in how they invoke the checker."""
        criterion_type = criterion.type
        result.pass_threshold = criterion.pass_threshold
        result.gating = criterion.is_gating

        logger.debug(f"Criterion '{criterion_type}' score: {result.score:.2f}")
        if result.score < criterion.pass_threshold:
            # An informational (weight: 0) criterion cannot fail the task, so
            # logging it as FAILED contradicts the final status. Say what it is.
            logger.info(
                "Criterion '%s' %s (score=%.2f, threshold=%.2f): %s",
                criterion_type,
                "FAILED" if criterion.is_gating else "below threshold (informational)",
                result.score,
                criterion.pass_threshold,
                _short_failure_reason(result),
            )
        return result

    def _missing_checker_result(self, criterion: SuccessCriterion) -> CriterionResult:
        """Failed result for an unregistered criterion type. Shared by the sync/async
        KeyError branches in ``_check_single`` / ``_check_single_async``."""
        criterion_type = criterion.type
        # ERROR record carries enough context — skip the companion FAILED line.
        logger.error(f"No checker found for criterion type '{criterion_type}'")
        return CriterionResult(
            criterion_type=criterion_type,
            description=criterion.description,
            score=0.0,
            details=f"No checker registered for criterion type '{criterion_type}'",
            error=f"Unsupported criterion type: '{criterion_type}'",
            pass_threshold=criterion.pass_threshold,
            gating=criterion.is_gating,
        )

    def _error_result(self, criterion: SuccessCriterion, exc: Exception) -> CriterionResult:
        """Failed result for any other checker exception, including checker __init__
        failures. Shared by the sync/async generic-Exception branches in
        ``_check_single`` / ``_check_single_async``."""
        criterion_type = criterion.type
        logger.exception(f"Checker failure for criterion '{criterion_type}': {exc}")
        failed = CriterionResult(
            criterion_type=criterion_type,
            description=criterion.description,
            score=0.0,
            details="Error running checker",
            error=str(exc),
            pass_threshold=criterion.pass_threshold,
            gating=criterion.is_gating,
        )
        logger.info(
            "Criterion '%s' %s (score=0.00, threshold=%.2f): %s",
            criterion_type,
            "FAILED" if criterion.is_gating else "errored (informational)",
            criterion.pass_threshold,
            _short_failure_reason(failed),
        )
        return failed

    def _check_single(
        self,
        criterion: SuccessCriterion,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriterionResult:
        """Check a single criterion using registered checker.

        Args:
            criterion: Criterion definition (discriminated union)
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory.

        Returns:
            CriterionResult with score
        """
        # V3: Broader exception handling - catches checker constructor failures too
        try:
            checker = self._get_checker_instance(criterion.type)
            context = CheckContext(route=self.route, reference_dir=reference_dir)
            result = checker.check(
                criterion,
                self.sandbox,
                turn_records=turn_records,
                context=context,
            )
            return self._finalize_result(criterion, result)
        except KeyError:
            return self._missing_checker_result(criterion)
        except _ESCALATING_EXCEPTIONS:
            raise  # judge infra failure / checker misuse escalates to FinalStatus.ERROR; do not score it
        except Exception as e:
            # V3: Catch ALL exceptions, including checker __init__ failures
            return self._error_result(criterion, e)

    async def _check_single_async(
        self,
        criterion: SuccessCriterion,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriterionResult:
        """Async twin of ``_check_single`` — used only for native-async checkers,
        dispatched via ``check_async`` instead of ``check``. Shares the result
        shaping and logging via ``_finalize_result`` / ``_missing_checker_result`` /
        ``_error_result``, so the two methods differ only in how they invoke the
        checker (``checker.check`` vs. ``await checker.check_async``).
        """
        try:
            checker = self._get_checker_instance(criterion.type)
            context = CheckContext(route=self.route, reference_dir=reference_dir)
            result = await checker.check_async(
                criterion,
                self.sandbox,
                turn_records=turn_records,
                context=context,
            )
            return self._finalize_result(criterion, result)
        except KeyError:
            # Deliberate dead-code defence: unreachable today, kept so the two
            # `_check_single*` methods' exception shape stays identical.
            return self._missing_checker_result(criterion)
        except _ESCALATING_EXCEPTIONS:
            raise
        except Exception as e:
            return self._error_result(criterion, e)
