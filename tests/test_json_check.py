"""Tests for the json_check criterion."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from coder_eval.criteria.json_check import JsonCheckChecker
from coder_eval.models import JMESPathAssertion, JsonCheckCriterion
from coder_eval.sandbox import Sandbox


class TestJMESPathAssertionModel:
    """Verify JMESPathAssertion model defaults and validation."""

    def test_defaults_with_expected(self):
        a = JMESPathAssertion(expression="status", expected="ok")
        assert a.operator == "equals"
        assert a.expected == "ok"

    def test_exists_defaults_no_expected(self):
        a = JMESPathAssertion(expression="name", operator="exists")
        assert a.operator == "exists"
        assert a.expected is None

    def test_explicit_operator(self):
        a = JMESPathAssertion(expression="length(items)", operator="gte", expected=3)
        assert a.operator == "gte"
        assert a.expected == 3

    def test_exists_operator_no_expected(self):
        a = JMESPathAssertion(expression="name", operator="exists")
        assert a.expected is None

    def test_equals_without_expected_raises(self):
        with pytest.raises(ValidationError, match="expected"):
            JMESPathAssertion(expression="status", operator="equals")

    def test_gt_without_expected_raises(self):
        with pytest.raises(ValidationError, match="expected"):
            JMESPathAssertion(expression="count", operator="gt")

    def test_regex_without_expected_raises(self):
        with pytest.raises(ValidationError, match="expected"):
            JMESPathAssertion(expression="version", operator="regex")

    def test_equals_with_expected_none_accepted(self):
        """expected=None (JSON null) is a valid value for operators like equals/not_equals."""
        a = JMESPathAssertion(expression="key", operator="equals", expected=None)
        assert a.expected is None
        assert a.operator == "equals"

    def test_not_equals_with_expected_none_accepted(self):
        a = JMESPathAssertion(expression="key", operator="not_equals", expected=None)
        assert a.expected is None


class TestJsonCheckCriterionModel:
    """Verify JsonCheckCriterion model defaults and construction."""

    def test_defaults(self):
        c = JsonCheckCriterion(description="d", path="data.json")
        assert c.type == "json_check"
        assert c.json_schema is None
        assert c.assertions == []

    def test_schema_only(self):
        c = JsonCheckCriterion(description="d", path="data.json", json_schema="schemas/output.json")
        assert c.json_schema == "schemas/output.json"

    def test_assertions_only(self):
        c = JsonCheckCriterion(
            description="d",
            path="data.json",
            assertions=[
                JMESPathAssertion(expression="status", expected="success"),
                JMESPathAssertion(expression="length(items)", operator="gte", expected=1),
            ],
        )
        assert len(c.assertions) == 2

    def test_schema_and_assertions(self):
        c = JsonCheckCriterion(
            description="d",
            path="data.json",
            json_schema="schemas/output.json",
            assertions=[JMESPathAssertion(expression="status", expected="ok")],
        )
        assert c.json_schema is not None
        assert len(c.assertions) == 1

    def test_discriminator_type(self):
        """Verify the type field works as discriminator in the union."""
        c = JsonCheckCriterion(description="d", path="data.json")
        assert c.type == "json_check"


class TestJsonCheckScoring:
    """Verify scoring logic with mocked sandbox."""

    def _sandbox(self, files: dict[str, str] | None = None) -> MagicMock:
        """Create a mocked sandbox with optional file contents."""
        s = MagicMock(spec=Sandbox)
        if files is None:
            s.file_exists.return_value = False
        else:
            s.file_exists.side_effect = lambda p: p in files
            s.get_file_content.side_effect = lambda p: files[p]
        return s

    def test_file_not_found(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json")
        result = checker._check_impl(c, self._sandbox(None))
        assert result.score == 0.0
        assert "does not exist" in result.error

    def test_invalid_json(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json")
        result = checker._check_impl(c, self._sandbox({"x.json": "not json{"}))
        assert result.score == 0.0
        assert "Invalid JSON" in result.error

    def test_valid_json_no_checks(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json")
        result = checker._check_impl(c, self._sandbox({"x.json": '{"key": "value"}'}))
        assert result.score == 1.0
        assert "valid JSON" in result.details

    def test_assertions_all_pass(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[
                JMESPathAssertion(expression="status", expected="success"),
                JMESPathAssertion(expression="count", operator="gte", expected=5),
            ],
        )
        data = '{"status": "success", "count": 10}'
        result = checker._check_impl(c, self._sandbox({"x.json": data}))
        assert result.score == 1.0

    def test_assertions_partial(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[
                JMESPathAssertion(expression="status", expected="success"),
                JMESPathAssertion(expression="status", expected="failure"),
            ],
        )
        data = '{"status": "success"}'
        result = checker._check_impl(c, self._sandbox({"x.json": data}))
        assert result.score == pytest.approx(0.5)

    def test_assertions_none_pass(self):
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[JMESPathAssertion(expression="status", expected="done")],
        )
        data = '{"status": "pending"}'
        result = checker._check_impl(c, self._sandbox({"x.json": data}))
        assert result.score == 0.0


class TestJsonCheckOperators:
    """Test each operator individually."""

    def _sandbox(self, data: dict) -> MagicMock:
        s = MagicMock(spec=Sandbox)
        s.file_exists.return_value = True
        s.get_file_content.return_value = json.dumps(data)
        return s

    def _check(self, data: dict, expression: str, operator: str, expected: Any = None) -> float:
        checker = JsonCheckChecker()
        kwargs = {"expression": expression, "operator": operator}
        if operator != "exists":
            kwargs["expected"] = expected
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[JMESPathAssertion(**kwargs)],
        )
        return checker._check_impl(c, self._sandbox(data)).score

    def test_equals_pass(self):
        assert self._check({"s": "ok"}, "s", "equals", "ok") == 1.0

    def test_equals_fail(self):
        assert self._check({"s": "ok"}, "s", "equals", "no") == 0.0

    def test_not_equals_pass(self):
        assert self._check({"s": "ok"}, "s", "not_equals", "no") == 1.0

    def test_not_equals_fail(self):
        assert self._check({"s": "ok"}, "s", "not_equals", "ok") == 0.0

    def test_contains_string(self):
        assert self._check({"s": "hello world"}, "s", "contains", "world") == 1.0

    def test_contains_list(self):
        assert self._check({"items": [1, 2, 3]}, "items", "contains", 2) == 1.0

    def test_contains_fail(self):
        assert self._check({"s": "hello"}, "s", "contains", "xyz") == 0.0

    def test_contains_non_iterable_scores_zero(self):
        assert self._check({"n": 42}, "n", "contains", "x") == 0.0

    def test_gt_pass(self):
        assert self._check({"n": 10}, "n", "gt", 5) == 1.0

    def test_gt_fail(self):
        assert self._check({"n": 3}, "n", "gt", 5) == 0.0

    def test_gte_pass_equal(self):
        assert self._check({"n": 5}, "n", "gte", 5) == 1.0

    def test_lt_pass(self):
        assert self._check({"n": 3}, "n", "lt", 5) == 1.0

    def test_lte_pass_equal(self):
        assert self._check({"n": 5}, "n", "lte", 5) == 1.0

    def test_numeric_op_type_mismatch_scores_zero(self):
        assert self._check({"s": "abc"}, "s", "gt", 5) == 0.0

    def test_type_string(self):
        assert self._check({"s": "hi"}, "s", "type", "string") == 1.0

    def test_type_number_int(self):
        assert self._check({"n": 42}, "n", "type", "number") == 1.0

    def test_type_number_float(self):
        assert self._check({"n": 3.14}, "n", "type", "number") == 1.0

    def test_type_boolean(self):
        assert self._check({"b": True}, "b", "type", "boolean") == 1.0

    def test_type_array(self):
        assert self._check({"a": [1, 2]}, "a", "type", "array") == 1.0

    def test_type_object(self):
        assert self._check({"o": {"k": "v"}}, "o", "type", "object") == 1.0

    def test_type_null(self):
        assert self._check({"n": None}, "n", "type", "null") == 1.0

    def test_type_mismatch(self):
        assert self._check({"s": "hi"}, "s", "type", "number") == 0.0

    def test_type_bool_not_number(self):
        """Python bool is a subclass of int, but type operator must not classify bools as numbers."""
        assert self._check({"b": True}, "b", "type", "number") == 0.0
        assert self._check({"b": False}, "b", "type", "number") == 0.0

    def test_type_unsupported_name_scores_zero(self):
        """Unsupported type names (typos like 'integer') must fail, not silently match null."""
        assert self._check({"n": 42}, "n", "type", "integer") == 0.0
        assert self._check({"n": None}, "n", "type", "integer") == 0.0

    def test_equals_null_pass(self):
        """equals with expected=None should match JSON null values."""
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[JMESPathAssertion(expression="key", operator="equals", expected=None)],
        )
        assert checker._check_impl(c, self._sandbox({"key": None})).score == 1.0

    def test_equals_null_fail(self):
        """equals with expected=None should not match non-null values."""
        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            assertions=[JMESPathAssertion(expression="key", operator="equals", expected=None)],
        )
        assert checker._check_impl(c, self._sandbox({"key": "value"})).score == 0.0

    def test_regex_pass(self):
        assert self._check({"v": "v1.2.3"}, "v", "regex", r"^v\d+\.\d+\.\d+$") == 1.0

    def test_regex_fail(self):
        assert self._check({"v": "latest"}, "v", "regex", r"^v\d+") == 0.0

    def test_regex_non_string_scores_zero(self):
        assert self._check({"n": 42}, "n", "regex", r"\d+") == 0.0

    def test_exists_pass(self):
        assert self._check({"key": "value"}, "key", "exists") == 1.0

    def test_exists_fail_missing_key(self):
        assert self._check({"other": "value"}, "key", "exists") == 0.0

    def test_exists_null_value_fails(self):
        """JMESPath returns None for both missing keys and explicit null."""
        assert self._check({"key": None}, "key", "exists") == 0.0

    def test_bad_jmespath_expression_scores_zero(self):
        assert self._check({"k": 1}, "invalid..expression", "exists") == 0.0


class TestJsonCheckSchema:
    """Test schema validation scoring."""

    def _sandbox(self, files: dict[str, str]) -> MagicMock:
        s = MagicMock(spec=Sandbox)
        s.file_exists.side_effect = lambda p: p in files
        s.get_file_content.side_effect = lambda p: files[p]
        return s

    def test_schema_valid(self):
        schema = json.dumps(
            {
                "type": "object",
                "required": ["name", "age"],
                "properties": {
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
            }
        )
        data = json.dumps({"name": "Alice", "age": 30})
        sandbox = self._sandbox({"x.json": data, "schema.json": schema})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json", json_schema="schema.json")
        result = checker._check_impl(c, sandbox)
        assert result.score == 1.0
        assert "Schema: valid" in result.details

    def test_schema_invalid(self):
        schema = json.dumps(
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }
        )
        data = json.dumps({"age": 30})
        sandbox = self._sandbox({"x.json": data, "schema.json": schema})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json", json_schema="schema.json")
        result = checker._check_impl(c, sandbox)
        assert result.score == 0.0
        assert "Schema: invalid" in result.details

    def test_schema_file_missing(self):
        data = json.dumps({"key": "value"})
        sandbox = self._sandbox({"x.json": data})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json", json_schema="missing.json")
        result = checker._check_impl(c, sandbox)
        assert result.score == 0.0
        assert "not found" in result.details

    def test_schema_file_invalid_json(self):
        data = json.dumps({"key": "value"})
        sandbox = self._sandbox({"x.json": data, "schema.json": "not json{"})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json", json_schema="schema.json")
        result = checker._check_impl(c, sandbox)
        assert result.score == 0.0
        assert "Invalid JSON in schema" in result.details

    def test_malformed_schema(self):
        data = json.dumps({"key": "value"})
        sandbox = self._sandbox({"x.json": data, "schema.json": json.dumps({"type": "invalid_type"})})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(description="d", path="x.json", json_schema="schema.json")
        result = checker._check_impl(c, sandbox)
        assert result.score == 0.0

    def test_schema_and_assertions_both_pass(self):
        schema = json.dumps({"type": "object", "required": ["status"]})
        data = json.dumps({"status": "ok", "count": 5})
        sandbox = self._sandbox({"x.json": data, "schema.json": schema})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            json_schema="schema.json",
            assertions=[JMESPathAssertion(expression="status", expected="ok")],
        )
        result = checker._check_impl(c, sandbox)
        assert result.score == 1.0

    def test_schema_pass_assertions_partial(self):
        schema = json.dumps({"type": "object"})
        data = json.dumps({"status": "ok"})
        sandbox = self._sandbox({"x.json": data, "schema.json": schema})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            json_schema="schema.json",
            assertions=[
                JMESPathAssertion(expression="status", expected="ok"),
                JMESPathAssertion(expression="status", expected="fail"),
            ],
        )
        result = checker._check_impl(c, sandbox)
        # schema=1.0, assertions=0.5 -> average=0.75
        assert result.score == pytest.approx(0.75)

    def test_schema_fail_gates_assertions(self):
        """Schema failure gates assertions — score is 0.0, assertions are skipped."""
        schema = json.dumps({"type": "object", "required": ["missing_key"]})
        data = json.dumps({"status": "ok"})
        sandbox = self._sandbox({"x.json": data, "schema.json": schema})

        checker = JsonCheckChecker()
        c = JsonCheckCriterion(
            description="d",
            path="x.json",
            json_schema="schema.json",
            assertions=[JMESPathAssertion(expression="status", expected="ok")],
        )
        result = checker._check_impl(c, sandbox)
        assert result.score == 0.0
        assert "skipped" in (result.details or "")


class TestJsonCheckIntegration:
    """Integration tests with real sandbox."""

    def test_valid_json_no_checks(self):
        from coder_eval.evaluation.checker import SuccessChecker
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox as RealSandbox

        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = RealSandbox(config, task_id="test_jc_valid")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "data.json").write_text('{"key": "value"}')

        checker = SuccessChecker(sandbox)
        result = checker.check(JsonCheckCriterion(description="valid json", path="data.json"))

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_assertions_with_real_sandbox(self):
        from coder_eval.evaluation.checker import SuccessChecker
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox as RealSandbox

        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = RealSandbox(config, task_id="test_jc_assert")
        sandbox_dir = sandbox.setup()
        (sandbox_dir / "report.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "items": [{"name": "a"}, {"name": "b"}],
                    "version": "v2.1.0",
                }
            )
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(
            JsonCheckCriterion(
                description="report check",
                path="report.json",
                assertions=[
                    JMESPathAssertion(expression="status", expected="success"),
                    JMESPathAssertion(expression="length(items)", operator="gte", expected=2),
                    JMESPathAssertion(expression="version", operator="regex", expected=r"^v\d+\.\d+"),
                ],
            )
        )

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_schema_with_real_sandbox(self):
        from coder_eval.evaluation.checker import SuccessChecker
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox as RealSandbox

        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = RealSandbox(config, task_id="test_jc_schema")
        sandbox_dir = sandbox.setup()

        schema = {"type": "object", "required": ["id", "name"]}
        (sandbox_dir / "schema.json").write_text(json.dumps(schema))
        (sandbox_dir / "data.json").write_text(json.dumps({"id": 1, "name": "test"}))

        checker = SuccessChecker(sandbox)
        result = checker.check(
            JsonCheckCriterion(
                description="schema check",
                path="data.json",
                json_schema="schema.json",
            )
        )

        assert result.score == 1.0
        sandbox.cleanup(preserve=False)

    def test_file_missing(self):
        from coder_eval.evaluation.checker import SuccessChecker
        from coder_eval.models import SandboxConfig
        from coder_eval.sandbox import Sandbox as RealSandbox

        config = SandboxConfig(driver="tempdir", python=None)
        sandbox = RealSandbox(config, task_id="test_jc_miss")
        sandbox.setup()

        checker = SuccessChecker(sandbox)
        result = checker.check(JsonCheckCriterion(description="missing", path="nope.json"))

        assert result.score == 0.0
        assert "does not exist" in result.error
        sandbox.cleanup(preserve=False)
