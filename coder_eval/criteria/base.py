"""Base criterion checker interface with error handling."""

import logging
import os
import traceback
from abc import ABC, abstractmethod
from functools import wraps
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from coder_eval.models import BaseSuccessCriterion, CriterionResult


if TYPE_CHECKING:
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# TypeVar for generic checker type safety
C = TypeVar("C", bound=BaseSuccessCriterion)


def handle_criterion_errors(func):
    """Decorator to handle errors in criterion checkers.

    Wraps checker methods to catch exceptions and return a failed
    CriterionResult with error details instead of raising.

    This is the CENTRALIZED error handling that was in evaluator.py.
    """

    @wraps(func)
    def wrapper(
        self,
        criterion: BaseSuccessCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
    ) -> CriterionResult:
        try:
            return func(self, criterion, sandbox, reference_code)
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


class BaseCriterion(ABC, Generic[C]):
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
    ) -> CriterionResult:
        """Execute the criterion check with centralized error handling.

        This method is FINAL - subclasses must NOT override it.
        Implement _check_impl() instead.

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison

        Returns:
            CriterionResult with score (0.0-1.0), details, and error info
        """
        return self._check_impl(criterion, sandbox, reference_code)

    @abstractmethod
    def _check_impl(
        self,
        criterion: C,
        sandbox: "Sandbox",
        reference_code: str | None = None,
    ) -> CriterionResult:
        """Implement the actual criterion checking logic.

        Subclasses override this method, NOT check().

        Args:
            criterion: The specific criterion definition (Pydantic model)
            sandbox: Sandbox instance for file access and command execution
            reference_code: Optional reference code string for comparison

        Returns:
            CriterionResult with score (0.0-1.0), details, and error info

        Raises:
            Any exception - will be caught by @handle_criterion_errors decorator
        """
        pass


# Decorator for registration (defined here to avoid circular imports)
def register_criterion(cls: type[BaseCriterion]) -> type[BaseCriterion]:
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
