"""Base criterion checker interface with error handling."""

import logging
import os
import statistics
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar

from coder_eval.errors import JudgeInfrastructureError
from coder_eval.models import BaseSuccessCriterion, CriterionAggregate, CriterionResult


if TYPE_CHECKING:
    from pathlib import Path

    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


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


def handle_criterion_errors(func: Callable[..., CriterionResult]) -> Callable[..., CriterionResult]:
    """Decorator to handle errors in criterion checkers.

    Wraps checker methods to catch exceptions and return a failed
    CriterionResult with error details instead of raising.

    This is the CENTRALIZED error handling that was in evaluator.py.
    """

    @wraps(func)
    def wrapper(
        self: Any,
        criterion: BaseSuccessCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        try:
            return func(
                self,
                criterion,
                sandbox,
                reference_code,
                turn_records=turn_records,
                context=context,
            )
        except JudgeInfrastructureError:
            # Judge infra failure is NOT an agent failure — do not score it 0.0.
            # Propagates to Orchestrator.run()'s broad except → FinalStatus.ERROR.
            raise
        except Exception as e:
            exc_info = f"{e.__class__.__name__}: {e}"
            tb = ""
            if os.getenv("CODER_EVAL_DEBUG") == "1":
                tb = "\n" + "".join(traceback.format_exc(limit=5))

            criterion_type = criterion.type
            logger.error(
                f"Error in {self.__class__.__name__}.check() for criterion type '{criterion_type}': {exc_info}",
                exc_info=True,  # Adds full stack trace to logs
            )
            return CriterionResult(
                criterion_type=criterion_type,
                description=criterion.description,
                score=0.0,
                details=f"Error during check: {exc_info}{tb}",
                error=exc_info,  # Include exception type and message
            )

    return wrapper


class BaseCriterion[C: BaseSuccessCriterion](ABC):
    """Abstract base class for all criterion checkers.

    Each criterion checker must:
    1. Define `criterion_type` as a ClassVar[str] matching the discriminator
    2. Implement `_check_impl()` with the actual checking logic

    The `check()` method is final and applies centralized error handling.

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
        Implement _check_impl() instead.

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

    @abstractmethod
    def _check_impl(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        *,
        turn_records: list["TurnRecord"] | None = None,
        context: "CheckContext | None" = None,
    ) -> CriterionResult:
        """Implement the actual criterion checking logic.

        Subclasses override this method, NOT check(). Every checker shares this
        uniform signature; non-judge checkers accept ``context`` and ignore it.

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
            Any exception - will be caught by @handle_criterion_errors decorator
        """
        pass

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

    Usage:
        @register_criterion
        class MyChecker(BaseCriterion[MyCriterion]):
            criterion_type = "my_type"
            ...
    """
    from coder_eval.criteria import CriterionRegistry

    return CriterionRegistry.register(cls)
