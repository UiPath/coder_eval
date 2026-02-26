"""Criterion checker registry and plugin system with dynamic discovery."""

# pyright: reportImportCycles=false

import importlib
import logging
import pkgutil
from typing import ClassVar, get_args

from coder_eval.criteria.base import BaseCriterion, handle_criterion_errors, register_criterion


logger = logging.getLogger(__name__)


class CriterionRegistry:
    """Registry for criterion checker plugins with dynamic discovery."""

    _checkers: ClassVar[dict[str, type[BaseCriterion]]] = {}  # type: ignore[reportMissingTypeArgument]
    _discovered: ClassVar[bool] = False

    @classmethod
    def register(cls, checker_class: type[BaseCriterion]) -> type[BaseCriterion]:  # type: ignore[reportMissingTypeArgument]
        """Register a criterion checker.

        Args:
            checker_class: Checker class to register

        Returns:
            The checker class (for use as decorator)

        Raises:
            TypeError: If checker_class doesn't inherit from BaseCriterion or lacks criterion_type
        """
        # Ensure checker_class inherits from BaseCriterion
        if not issubclass(checker_class, BaseCriterion):
            raise TypeError(f"{checker_class.__name__} must inherit from BaseCriterion")

        # Access criterion_type as class variable (V3: simplified from property)
        criterion_type = getattr(checker_class, "criterion_type", None)
        if not isinstance(criterion_type, str) or not criterion_type:
            raise TypeError(f"{checker_class.__name__} must define class var 'criterion_type: ClassVar[str]'")

        if criterion_type in cls._checkers:
            logger.warning(
                f"Overwriting existing checker for '{criterion_type}': "
                + f"{cls._checkers[criterion_type].__name__} -> {checker_class.__name__}"
            )
        cls._checkers[criterion_type] = checker_class
        logger.debug(f"Registered criterion checker: {criterion_type} -> {checker_class.__name__}")
        return checker_class

    @classmethod
    def get_checker(cls, criterion_type: str) -> type[BaseCriterion]:  # type: ignore[reportMissingTypeArgument]
        """Get checker class for a criterion type.

        Args:
            criterion_type: The criterion type discriminator

        Returns:
            Checker class

        Raises:
            KeyError: If no checker registered for this type
        """
        if criterion_type not in cls._checkers:
            raise KeyError(
                f"No checker registered for criterion type '{criterion_type}'. "
                + f"Available types: {list(cls._checkers.keys())}"
            )
        return cls._checkers[criterion_type]

    @classmethod
    def list_types(cls) -> list[str]:
        """List all registered criterion types."""
        return list(cls._checkers.keys())

    @classmethod
    def discover(cls) -> None:
        """Dynamically discover and register all criterion checkers.

        Uses pkgutil to auto-discover all modules in the criteria package.
        This eliminates the need to maintain a hardcoded list of modules.

        Provides error recovery - if a single checker import fails, others
        still register successfully.
        """
        if cls._discovered:
            logger.debug("Criteria already discovered, skipping")
            return

        # Dynamically import all modules in this package
        package = importlib.import_module(__name__)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__, f"{package.__name__}."):
            try:
                importlib.import_module(module_name)
                logger.debug(f"Successfully imported criteria module: {module_name}")
            except Exception as e:
                logger.error(f"Failed to import criteria module '{module_name}': {e}", exc_info=True)

        cls._discovered = True
        logger.debug(f"Discovered {len(cls._checkers)} criterion checkers")


def validate_registry() -> None:
    """Validate that all SuccessCriterion types have registered checkers.

    Dynamically introspects the Pydantic SuccessCriterion union to get
    expected types, ensuring validation stays in sync with the model.

    Raises:
        RuntimeError: If any criterion type lacks a checker
    """
    from coder_eval.models import SuccessCriterion

    # V3: Dynamically extract expected types from Pydantic union
    # This works with Pydantic v2's discriminated union
    try:
        # Get all submodels in the discriminated union using typing.get_args()
        # This is the stable public API for accessing union members
        union_members = get_args(SuccessCriterion)
        if not union_members:
            raise AttributeError("Could not extract union members")
        expected_types = {model.model_fields["type"].default for model in union_members}
    except (AttributeError, KeyError):
        # Fallback for different Pydantic versions
        logger.warning(
            "Could not dynamically extract criterion types from Pydantic union. Using static list as fallback."
        )
        expected_types = {
            "file_exists",
            "file_contains",
            "run_command",
            "program_stdout_equals",
            "pytest",
            "file_matches_regex",
            "code_lints",
            "pylint_score",
            "reference_comparison",
            "command_executed",
        }

    registered_types = set(CriterionRegistry.list_types())
    missing_types = expected_types - registered_types

    if missing_types:
        raise RuntimeError(f"Missing criterion checkers for types: {missing_types}. Registered: {registered_types}")

    logger.debug(f"Validated {len(registered_types)} criterion checkers")


def init_criteria(validate: bool = True) -> None:
    """Initialize the criteria registry.

    This should be called explicitly (e.g., in SuccessChecker.__init__),
    NOT at module import time. This keeps imports cheap and allows
    partial environments to function.

    Args:
        validate: Whether to validate all expected types are registered
    """
    CriterionRegistry.discover()
    if validate:
        validate_registry()


# Re-export for convenience
__all__ = [
    "BaseCriterion",
    "CriterionRegistry",
    "handle_criterion_errors",
    "init_criteria",
    "register_criterion",
    "validate_registry",
]
