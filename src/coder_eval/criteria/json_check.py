"""JSON check criterion checker (existence + parse + schema + JMESPath assertions)."""

import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import jmespath
import jsonschema

from coder_eval.criteria.base import BaseCriterion, register_criterion
from coder_eval.models import CriterionResult, JMESPathAssertion, JsonCheckCriterion


if TYPE_CHECKING:
    from coder_eval.models.results import TurnRecord
    from coder_eval.sandbox import Sandbox

logger = logging.getLogger(__name__)

# JSON type name -> Python type(s) mapping for the "type" operator.
# Task authors use JSON names (string, number, array, object, boolean, null).
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}

# Operator dispatch table. Each operator takes (actual, expected) and returns bool.
# All exceptions are caught per-assertion by the caller.
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "equals": lambda actual, expected: actual == expected,
    "not_equals": lambda actual, expected: actual != expected,
    "contains": lambda actual, expected: expected in actual,
    "gt": lambda actual, expected: actual > expected,
    "gte": lambda actual, expected: actual >= expected,
    "lt": lambda actual, expected: actual < expected,
    "lte": lambda actual, expected: actual <= expected,
    "type": lambda actual, expected: (
        expected in _JSON_TYPE_MAP
        and (
            (isinstance(actual, bool) and expected == "boolean")
            or (not isinstance(actual, bool) and isinstance(actual, _JSON_TYPE_MAP[expected]))
        )
    ),
    "regex": lambda actual, expected: bool(re.search(expected, actual)),
    "exists": lambda actual, _: actual is not None,
}


@register_criterion
class JsonCheckChecker(BaseCriterion[JsonCheckCriterion]):
    """Checker for JsonCheckCriterion."""

    criterion_type = "json_check"

    def _check_impl(
        self,
        criterion: JsonCheckCriterion,
        sandbox: "Sandbox",
        reference_code: str | None = None,
        turn_records: list["TurnRecord"] | None = None,
    ) -> CriterionResult:
        """JSON check: existence, parse, schema validation, JMESPath assertions."""
        # 1. File existence
        if not sandbox.file_exists(criterion.path):
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"File '{criterion.path}' does not exist",
            )

        # 2. Parse JSON
        content = sandbox.get_file_content(criterion.path)
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=0.0,
                error=f"Invalid JSON in '{criterion.path}': {e}",
            )

        has_schema = criterion.json_schema is not None
        has_assertions = len(criterion.assertions) > 0

        # 3. Pure validity check
        if not has_schema and not has_assertions:
            return CriterionResult(
                criterion_type=criterion.type,
                description=criterion.description,
                score=1.0,
                details=f"'{criterion.path}' is valid JSON",
            )

        scores: list[float] = []
        details_parts: list[str] = []

        # 4. Schema validation (gates assertions — if schema fails, skip assertions)
        if has_schema:
            assert criterion.json_schema is not None  # narrowing for pyright
            schema_score, schema_detail = self._check_schema(criterion.json_schema, data, sandbox)
            scores.append(schema_score)
            details_parts.append(schema_detail)

            if schema_score == 0.0 and has_assertions:
                details_parts.append("Assertions: skipped (schema failed)")
                score = 0.0
                details_parts.append(f"Score: {score:.2f}")
                return CriterionResult(
                    criterion_type=criterion.type,
                    description=criterion.description,
                    score=score,
                    details="; ".join(details_parts),
                )

        # 5. JMESPath assertions
        if has_assertions:
            assertions_score, assertions_detail = self._check_assertions(criterion.assertions, data)
            scores.append(assertions_score)
            details_parts.append(assertions_detail)

        # 6. Average active categories
        score = sum(scores) / len(scores)
        details_parts.append(f"Score: {score:.2f}")

        return CriterionResult(
            criterion_type=criterion.type,
            description=criterion.description,
            score=score,
            details="; ".join(details_parts),
        )

    def _check_schema(self, schema_path: str, data: Any, sandbox: "Sandbox") -> tuple[float, str]:
        """Validate data against a JSON Schema file."""
        if not sandbox.file_exists(schema_path):
            return 0.0, f"Schema file '{schema_path}' not found"

        schema_content = sandbox.get_file_content(schema_path)
        try:
            schema = json.loads(schema_content)
        except (json.JSONDecodeError, ValueError) as e:
            return 0.0, f"Invalid JSON in schema file '{schema_path}': {e}"

        try:
            jsonschema.validate(instance=data, schema=schema)
            return 1.0, "Schema: valid"
        except jsonschema.ValidationError as e:
            return 0.0, f"Schema: invalid — {e.message}"
        except jsonschema.SchemaError as e:
            return 0.0, f"Schema: malformed schema — {e.message}"

    def _check_assertions(self, assertions: list[JMESPathAssertion], data: Any) -> tuple[float, str]:
        """Evaluate JMESPath assertions against parsed JSON data."""
        passed = 0
        total = len(assertions)
        failed_details: list[str] = []

        for assertion in assertions:
            try:
                actual = jmespath.search(assertion.expression, data)
                op_func = _OPERATORS[assertion.operator]
                if op_func(actual, assertion.expected):
                    passed += 1
                else:
                    expr = assertion.expression
                    op = assertion.operator
                    failed_details.append(f"'{expr}' {op}: expected {assertion.expected!r}, got {actual!r}")
            except Exception as e:
                failed_details.append(f"'{assertion.expression}': error — {e}")

        score = passed / total
        detail = f"Assertions: {passed}/{total} passed"
        if failed_details:
            detail += f" ({'; '.join(failed_details)})"
        return score, detail
