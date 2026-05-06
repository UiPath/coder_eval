"""Success criterion checking with pluggable criterion types.

This module provides the SuccessChecker class which orchestrates
criterion checking using the registered criterion checkers from
the criteria registry.
"""

import logging
from typing import TYPE_CHECKING, Any

from ..criteria import BaseCriterion, CriterionRegistry, init_criteria
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
                Proxy / Bedrock) as the main coding agent. ``None`` is acceptable
                for non-sub-agent criteria; ``agent_judge`` requires a route.
        """
        self.sandbox = sandbox
        self._checker_instances: dict[str, BaseCriterion[Any]] = {}
        # Cached reference code - automatically set by check()/check_all() when provided
        # Used by subsequent check() calls that don't explicitly pass reference_code
        self._reference_code: str | None = None
        # Cached turn records - set by check()/check_all() when provided
        self._turn_records: TurnRecords | None = None
        self.route = route

        # V3: Lazy initialization - registry loaded here, not at import
        if init_registry:
            init_criteria(validate=validate_registry)

    def check(
        self,
        criterion: SuccessCriterion,
        reference_code: str | None = None,
        turn_records: TurnRecords | None = None,
    ) -> CriterionResult:
        """Check a single criterion (backward compatibility wrapper).

        Args:
            criterion: Criterion definition
            reference_code: Optional reference code for comparison
            turn_records: Optional turn records for command inspection

        Returns:
            CriterionResult with score
        """
        # Persist reference_code for subsequent calls (backward compat)
        if reference_code is not None:
            self._reference_code = reference_code
        if turn_records is not None:
            self._turn_records = turn_records
        # Use instance variable if no reference_code provided
        ref_code = reference_code if reference_code is not None else self._reference_code
        records = turn_records if turn_records is not None else self._turn_records
        return self._check_single(criterion, ref_code, records)

    def check_all(
        self,
        criteria: SuccessCriteria,
        reference_code: str | None = None,
        turn_records: TurnRecords | None = None,
    ) -> CriteriaResults:
        """Check all success criteria.

        Args:
            criteria: List of criterion definitions
            reference_code: Optional reference code for comparison
            turn_records: Optional turn records for command inspection

        Returns:
            List of criterion results with scores
        """
        # Persist reference_code for subsequent check() calls
        if reference_code is not None:
            self._reference_code = reference_code
        if turn_records is not None:
            self._turn_records = turn_records
        ref_code = reference_code if reference_code is not None else self._reference_code
        records = turn_records if turn_records is not None else self._turn_records
        results = []
        for criterion in criteria:
            result = self._check_single(criterion, ref_code, records)
            results.append(result)
        return results

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

    def _check_single(
        self,
        criterion: SuccessCriterion,
        reference_code: str | None,
        turn_records: TurnRecords | None = None,
    ) -> CriterionResult:
        """Check a single criterion using registered checker.

        Args:
            criterion: Criterion definition (discriminated union)
            reference_code: Optional reference code
            turn_records: Optional turn records for command inspection

        Returns:
            CriterionResult with score
        """
        criterion_type = criterion.type

        # V3: Broader exception handling - catches checker constructor failures too
        try:
            # Get cached instance
            checker = self._get_checker_instance(criterion_type)
            result = checker.check(criterion, self.sandbox, reference_code, turn_records=turn_records, route=self.route)
            result.pass_threshold = criterion.pass_threshold

            logger.debug(f"Criterion '{criterion_type}' score: {result.score:.2f}")
            if result.score < criterion.pass_threshold:
                logger.info(
                    "Criterion '%s' FAILED (score=%.2f, threshold=%.2f): %s",
                    criterion_type,
                    result.score,
                    criterion.pass_threshold,
                    _short_failure_reason(result),
                )
            return result

        except KeyError:
            # No checker registered for this type - return failed result for consistency.
            # ERROR record carries enough context — skip the companion FAILED line.
            logger.error(f"No checker found for criterion type '{criterion_type}'")
            return CriterionResult(
                criterion_type=criterion_type,
                description=criterion.description,
                score=0.0,
                details=f"No checker registered for criterion type '{criterion_type}'",
                error=f"Unsupported criterion type: '{criterion_type}'",
                pass_threshold=criterion.pass_threshold,
            )
        except Exception as e:
            # V3: Catch ALL exceptions, including checker __init__ failures
            logger.exception(f"Checker failure for criterion '{criterion_type}': {e}")
            failed = CriterionResult(
                criterion_type=criterion_type,
                description=criterion.description,
                score=0.0,
                details="Error running checker",
                error=str(e),
                pass_threshold=criterion.pass_threshold,
            )
            logger.info(
                "Criterion '%s' FAILED (score=0.00, threshold=%.2f): %s",
                criterion_type,
                criterion.pass_threshold,
                _short_failure_reason(failed),
            )
            return failed
