# json_check Criterion — Design Spec

## Problem

The previous `json_check` criterion was removed because it only supported flat key presence checks and exact value matching with dot-notation. Real-world eval tasks need richer JSON validation: schema conformance, nested structure queries, and flexible assertions on extracted values.

## Solution

A redesigned `json_check` criterion that leverages two established JSON tools:

- **JSON Schema** — structural validation ("does this JSON conform to a shape?")
- **JMESPath** — targeted value assertions ("does this specific value match?")

These are complementary categories. Only active categories contribute to the score.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Query language | JMESPath over JSONPath | Single canonical Python lib (AWS-maintained), zero transitive deps, deterministic typed output, built-in functions (`length`, `contains`, `type`) |
| Schema specification | File path only (no inline) | Keeps task YAML clean, schemas live as reusable artifacts in task templates |
| Schema scoring | Binary (1.0 / 0.0) | Schema errors cascade; fractional would be misleading. JMESPath assertions provide granularity. |
| Architecture | Single criterion (Approach 1) | Matches `file_check` pattern, simplest model and task YAML, one JSON parse per file |
| Operator extensibility | Dict-based dispatch | New operators are a one-line addition |
| Schema draft | Auto-detect from `$schema`, default Draft 2020-12 | Modern default, respects explicit declarations |
| `$ref` support | Not supported in v1 | Schemas must be self-contained; avoids resolver complexity |
| Field name | `json_schema` (not `schema`) | Avoids shadowing Pydantic v2's deprecated `BaseModel.schema()` method, which triggers `UserWarning` under strict pytest filterwarnings |
| Category weighting | Equal weight per category (schema, assertions) | Intentional — matches `file_check` pattern. Schema is a structural gate, assertions provide granular signal. |

## Data Model

### `JMESPathAssertion` (sub-model)

```python
class JMESPathAssertion(BaseModel):
    """A single JMESPath assertion within JsonCheckCriterion."""

    expression: str = Field(description="JMESPath expression to evaluate against the parsed JSON")
    operator: Literal[
        "equals", "not_equals", "contains", "gt", "gte", "lt", "lte", "type", "regex", "exists"
    ] = Field(default="equals", description="Comparison operator (default: 'equals')")
    expected: Any = Field(default=None, description="Expected value. Not required when operator is 'exists'.")
```

A `model_validator` enforces that `expected` is not None for all operators except `exists`. This prevents silent mis-evaluations from YAML typos (e.g., forgetting the `expected` field with `operator: "equals"`).

### `JsonCheckCriterion`

```python
class JsonCheckCriterion(BaseSuccessCriterion):
    """Validate a JSON file: existence, parseability, schema conformance, and JMESPath assertions.

    Fractional scoring. File missing or invalid JSON -> 0.0.
    Only active categories (schema, assertions) contribute to the average.
    """

    type: Literal["json_check"] = "json_check"
    path: str = Field(description="Path to the JSON file (relative to sandbox root)")
    json_schema: str | None = Field(default=None, description="Path to JSON Schema file (relative to sandbox root)")
    assertions: list[JMESPathAssertion] = Field(
        default_factory=list, description="JMESPath assertions to evaluate against the parsed JSON"
    )
```

Added to `SuccessCriterion` union in `models/criteria.py`.

## Checker Implementation

New file: `src/coder_eval/criteria/json_check.py`

### Schema File Loading

Schema files are loaded from the **sandbox** via `sandbox.get_file_content()`. Task templates place schema files into the sandbox (via `starter_files` or `template_dir`) before evaluation starts — the agent does not create them.

### Scoring Flow

```
1. sandbox.file_exists(path)?     -> No  -> score 0.0, error "file missing"
2. json.loads(content)?           -> No  -> score 0.0, error "invalid JSON"
3. No schema + no assertions?     -> Yes -> score 1.0, details "valid JSON"
4. Schema validation (if set):
   a. Load schema file via sandbox.get_file_content(json_schema)
   b. Parse schema JSON (file missing or invalid JSON -> schema_score 0.0)
   c. jsonschema.validate(data, schema) using Draft 2020-12 default
   d. Binary: 1.0 if valid, 0.0 if not
5. JMESPath assertions (if set):
   - For each assertion: jmespath.search(expr, data) -> apply operator
   - Score = passed / total
6. Final score = average of active category scores
```

### Operator Dispatch

```python
# JSON type name -> Python type mapping for the "type" operator
JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": (int, float),  # JSON "number" covers both
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}

OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "equals": lambda actual, expected: actual == expected,
    "not_equals": lambda actual, expected: actual != expected,
    "contains": lambda actual, expected: expected in actual,  # strings, lists, dicts
    "gt": lambda actual, expected: actual > expected,
    "gte": lambda actual, expected: actual >= expected,
    "lt": lambda actual, expected: actual < expected,
    "lte": lambda actual, expected: actual <= expected,
    "type": lambda actual, expected: isinstance(actual, JSON_TYPE_MAP.get(expected, type(None))),
    "regex": lambda actual, expected: bool(re.search(expected, actual)),  # strings only
    "exists": lambda actual, _: actual is not None,
}
```

### Error Handling

All operator exceptions (TypeError, KeyError, etc.) are caught **per-assertion** and scored 0.0 for that assertion with a diagnostic detail message. This means:

- **`contains`** on a non-iterable (int, float, bool, None) → assertion scores 0.0
- **`gt`/`gte`/`lt`/`lte`** on incomparable types → assertion scores 0.0
- **`regex`** on a non-string value → assertion scores 0.0 (no implicit `str()` conversion)
- **Bad JMESPath expression** → assertion scores 0.0

The `@handle_criterion_errors` decorator on `check()` catches any remaining unexpected errors at the criterion level.

### `exists` Operator Semantics

JMESPath returns `None` for both missing keys and keys explicitly set to JSON `null`. The `exists` operator checks `actual is not None`, so it **cannot distinguish missing keys from explicit null values**. Task authors who need to differentiate should use a JMESPath `keys()` expression or `contains()` function instead (e.g., `expression: "contains(keys(@), 'mykey')"`).

## Files Changed

| File | Change |
|------|--------|
| `src/coder_eval/models/criteria.py` | Add `JMESPathAssertion`, `JsonCheckCriterion`, update `SuccessCriterion` union |
| `src/coder_eval/criteria/json_check.py` | New checker file with `@register_criterion` |
| `src/coder_eval/models/__init__.py` | Export `JMESPathAssertion`, `JsonCheckCriterion` |
| `pyproject.toml` | Add `jmespath`, `jsonschema` runtime dependencies |
| `tests/` | Unit tests for model validation + checker logic |

No changes to registry, auto-discovery, or checker dispatch — the plugin system handles it.

## New Dependencies

| Package | Purpose | Transitive deps |
|---------|---------|-----------------|
| `jmespath` | JMESPath query evaluation | None |
| `jsonschema` | JSON Schema validation | `attrs`, `referencing`, `jsonschema-specifications` |

## Task YAML Examples

```yaml
# Minimal: just validate JSON syntax
- type: "json_check"
  path: "data.json"
  description: "data.json is valid JSON"

# Schema only
- type: "json_check"
  path: "output.json"
  json_schema: "schemas/output_schema.json"
  description: "Output conforms to expected schema"

# Assertions only
- type: "json_check"
  path: "report.json"
  assertions:
    - expression: "status"
      expected: "success"
    - expression: "length(results)"
      operator: "gte"
      expected: 1
    - expression: "metadata.version"
      operator: "regex"
      expected: "^\\d+\\.\\d+\\.\\d+$"
  description: "Report has correct structure and values"

# Both schema + assertions
- type: "json_check"
  path: "result.json"
  json_schema: "schemas/result_schema.json"
  assertions:
    - expression: "status"
      expected: "completed"
    - expression: "items[?active].name"
      operator: "exists"
  description: "Result is valid and has expected values"
```

## Scoring Summary

| Scenario | Score |
|----------|-------|
| File missing | 0.0 |
| Invalid JSON | 0.0 |
| Valid JSON, no sub-checks | 1.0 |
| Schema only (valid) | 1.0 |
| Schema only (invalid / schema file missing) | 0.0 |
| Assertions only | passed / total |
| Both | (schema_score + assertions_score) / 2 |
