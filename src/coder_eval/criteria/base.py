"""Base criterion checker interface with error handling."""

import inspect
import logging
import os
import statistics
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, ClassVar

from coder_eval.models import BaseSuccessCriterion, CriterionAggregate, CriterionResult


if TYPE_CHECKING:
    from pathlib import Path

    from coder_eval.models.results import TurnRecord
    from coder_eval.models.routing import ApiRoute
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)


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
        route: "ApiRoute | None" = None,
        reference_dir: "Path | None" = None,
    ) -> CriterionResult:
        try:
            return func(
                self,
                criterion,
                sandbox,
                reference_code,
                turn_records=turn_records,
                route=route,
                reference_dir=reference_dir,
            )
        except Exception as e:
            exc_info = f"{e.__class__.__name__}: {e}"
            tb = ""
            if os.getenv("CODER_EVAL_DEBUG") == "1":
                tb = "\n" + "".join(traceback.format_exc(limit=5))

            criterion_type = getattr(criterion, "type", "unknown")
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

    # Per-subclass cache of which kwargs the subclass's ``_check_impl`` accepts.
    # Lets the base ``check`` forward forward-compat kwargs (e.g. ``reference_dir``,
    # added when directory references were introduced) without forcing every
    # subclass to declare them. Filled lazily in ``check()``.
    _impl_accepted_params: ClassVar[set[str] | None] = None

    @handle_criterion_errors
    def check(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        route: "ApiRoute | None" = None,
        reference_dir: "Path | None" = None,
    ) -> CriterionResult:
        """Execute the criterion check with centralized error handling.

        This method is FINAL - subclasses must NOT override it.
        Implement _check_impl() instead.

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison
            turn_records: Optional list of turn records for command inspection
            route: Optional resolved ApiRoute. Forwarded by SuccessChecker so
                criteria that spawn sub-agents (e.g. agent_judge) can route
                through the same backend (Direct/Proxy/Bedrock) as the main
                agent. Most checkers ignore it.
            reference_dir: Optional resolved path to a reference directory
                (from ``task.reference.directory``). Only consumed by
                ``agent_judge``; non-judge checkers ignore it.

        Returns:
            CriterionResult with score (0.0-1.0), details, and error info
        """
        # Forward only the kwargs the subclass's _check_impl actually accepts.
        # Older checkers haven't been updated to take ``reference_dir`` and would
        # raise TypeError; the filter keeps them oblivious to the new field.
        cls = type(self)
        if cls._impl_accepted_params is None:
            cls._impl_accepted_params = set(inspect.signature(cls._check_impl).parameters)
        accepted = cls._impl_accepted_params

        kwargs: dict[str, Any] = {"turn_records": turn_records, "route": route}
        if "reference_dir" in accepted:
            kwargs["reference_dir"] = reference_dir
        return self._check_impl(criterion, sandbox, reference_code, **kwargs)

    @abstractmethod
    def _check_impl(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
        route: "ApiRoute | None" = None,
    ) -> CriterionResult:
        """Implement the actual criterion checking logic.

        Subclasses override this method, NOT check().

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison
            turn_records: Optional list of turn records for command inspection
            route: Optional resolved ApiRoute (see ``check()``).

        Subclasses may also declare ``reference_dir: Path | None = None`` —
        ``check()`` introspects each subclass's signature and forwards the
        kwarg only when accepted, so older subclasses don't have to update.
        Currently only ``agent_judge`` consumes ``reference_dir``.

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
        # `type` is a Literal on every concrete subclass of BaseSuccessCriterion,
        # but the bound doesn't expose it — reach through getattr for pyright.
        criterion_type = getattr(criterion, "type", "unknown")
        return CriterionAggregate(
            criterion_type=criterion_type,
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
