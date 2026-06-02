"""Success criterion checking with pluggable criterion types.

This module provides the SuccessChecker class which orchestrates
criterion checking using the registered criterion checkers from
the criteria registry.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..criteria import BaseCriterion, CriterionRegistry, init_criteria
from ..criteria.base import CheckContext
from ..models import CriteriaResults, CriterionResult, SuccessCriteria, SuccessCriterion, TurnRecords
from ..sandbox import Sandbox


if TYPE_CHECKING:
    from ..models.routing import ApiRoute
    from ..proxy.server import LLMGatewayProxy


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
        proxy: "LLMGatewayProxy | None" = None,
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
            proxy: Optional reference to the active ``LLMGatewayProxy``. Only set
                when ``route`` is a ``ProxyRoute``; ``None`` on Direct / Bedrock.
                Forwarded to checkers so judges (``agent_judge``, ``llm_judge``)
                can snapshot proxy usage and attribute their own slice instead
                of having their tokens pooled into the main agent's total via
                the orchestrator's zero-SDK fallback.
        """
        self.sandbox = sandbox
        self._checker_instances: dict[str, BaseCriterion[Any]] = {}
        # Cached reference code - automatically set by check()/check_all() when provided
        # Used by subsequent check() calls that don't explicitly pass reference_code
        self._reference_code: str | None = None
        # Cached reference directory path (resolved). Set by check()/check_all() when provided.
        # Mutually exclusive with self._reference_code at the task level — task.reference
        # is exactly one of code/file/directory.
        self._reference_dir: Path | None = None
        # Cached turn records - set by check()/check_all() when provided
        self._turn_records: TurnRecords | None = None
        self.route = route
        self.proxy = proxy

        # V3: Lazy initialization - registry loaded here, not at import
        if init_registry:
            init_criteria(validate=validate_registry)

    def check(
        self,
        criterion: SuccessCriterion,
        reference_code: str | None = None,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriterionResult:
        """Check a single criterion (backward compatibility wrapper).

        Args:
            criterion: Criterion definition
            reference_code: Optional reference code (string form: from
                ``task.reference.code`` or ``task.reference.file``).
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory
                (from ``task.reference.directory``). Only consumed by
                ``agent_judge``; non-judge criteria ignore it.

        Returns:
            CriterionResult with score
        """
        # Persist reference_code / reference_dir for subsequent calls (backward compat)
        if reference_code is not None:
            self._reference_code = reference_code
        if reference_dir is not None:
            self._reference_dir = reference_dir
        if turn_records is not None:
            self._turn_records = turn_records
        ref_code = reference_code if reference_code is not None else self._reference_code
        ref_dir = reference_dir if reference_dir is not None else self._reference_dir
        records = turn_records if turn_records is not None else self._turn_records
        return self._check_single(criterion, ref_code, records, ref_dir)

    def check_all(
        self,
        criteria: SuccessCriteria,
        reference_code: str | None = None,
        turn_records: TurnRecords | None = None,
        reference_dir: Path | None = None,
    ) -> CriteriaResults:
        """Check all success criteria.

        Args:
            criteria: List of criterion definitions
            reference_code: Optional reference code (string form).
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory.
                Only consumed by ``agent_judge``; non-judge criteria ignore it.

        Returns:
            List of criterion results with scores
        """
        # Persist for subsequent check() calls
        if reference_code is not None:
            self._reference_code = reference_code
        if reference_dir is not None:
            self._reference_dir = reference_dir
        if turn_records is not None:
            self._turn_records = turn_records
        ref_code = reference_code if reference_code is not None else self._reference_code
        ref_dir = reference_dir if reference_dir is not None else self._reference_dir
        records = turn_records if turn_records is not None else self._turn_records
        results = []
        for criterion in criteria:
            result = self._check_single(criterion, ref_code, records, ref_dir)
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
        reference_dir: Path | None = None,
    ) -> CriterionResult:
        """Check a single criterion using registered checker.

        Args:
            criterion: Criterion definition (discriminated union)
            reference_code: Optional reference code (string form)
            turn_records: Optional turn records for command inspection
            reference_dir: Optional resolved path to a reference directory.

        Returns:
            CriterionResult with score
        """
        criterion_type = criterion.type

        # V3: Broader exception handling - catches checker constructor failures too
        try:
            # Get cached instance
            checker = self._get_checker_instance(criterion_type)
            context = CheckContext(route=self.route, reference_dir=reference_dir, proxy=self.proxy)
            result = checker.check(
                criterion,
                self.sandbox,
                reference_code,
                turn_records=turn_records,
                context=context,
            )
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
