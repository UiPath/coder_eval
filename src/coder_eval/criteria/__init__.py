"""Criterion checker registry and plugin system with dynamic discovery."""

# pyright: reportImportCycles=false

import importlib
import logging
import pkgutil
from typing import Annotated, Any, ClassVar, get_args, get_origin

from pydantic.fields import FieldInfo

from coder_eval.criteria.base import BaseCriterion, handle_criterion_errors, register_criterion


logger = logging.getLogger(__name__)


class CriterionRegistry:
    """Registry for criterion checker plugins with dynamic discovery."""

    _checkers: ClassVar[dict[str, type[BaseCriterion[Any]]]] = {}
    _discovered: ClassVar[bool] = False

    @classmethod
    def register(cls, checker_class: type[BaseCriterion[Any]]) -> type[BaseCriterion[Any]]:
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
        return checker_class

    @classmethod
    def get_checker(cls, criterion_type: str) -> type[BaseCriterion[Any]]:
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
            except Exception as e:
                logger.error(f"Failed to import criteria module '{module_name}': {e}", exc_info=True)

        cls._discovered = True
        logger.debug(f"Discovered {len(cls._checkers)} criterion checkers")


def validate_registry() -> None:
    """Validate that all SuccessCriterion types have registered checkers.

    Introspects the SuccessCriterion discriminated union for the expected
    types, so validation stays in sync with the model. Fail-loud by design:
    there is no static fallback list — the previous fallback had already
    rotted (it was missing ``agent_judge``), proving a second source of
    truth cannot be trusted. If the union shape ever changes, this raises
    instead of silently validating against a stale set.

    Raises:
        RuntimeError: If the union is not a discriminated ``Annotated`` union,
            or if any criterion type lacks a checker
    """
    from coder_eval.models import SuccessCriterion

    if get_origin(SuccessCriterion) is not Annotated:
        raise RuntimeError("SuccessCriterion must be an Annotated discriminated union (Field(discriminator='type'))")
    inner, *metadata = get_args(SuccessCriterion)
    if not any(isinstance(m, FieldInfo) and m.discriminator == "type" for m in metadata):
        raise RuntimeError("SuccessCriterion union must declare Field(discriminator='type')")
    union_members = get_args(inner)
    if not union_members:
        raise RuntimeError("Could not extract SuccessCriterion union members")
    expected_types = {model.model_fields["type"].default for model in union_members}

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
