# Judge result discriminated union (`result_kind`)

**Date:** 2026-05-12
**Plan:** `c/2026-05-12-judge-code-review-fixes.md` Phase 1
**Affects:** `task.json` on-disk shape, HTML/MD report rendering.

## What changed

`EvaluationResult.success_criteria_results` is now typed as a discriminated union
`CriterionResultUnion`:

```python
CriterionResultUnion = Annotated[
    Annotated[CriterionResult,                Tag("basic")]
    | Annotated[JudgeCriterionResult,         Tag("judge")]
    | Annotated[ClassificationCriterionResult, Tag("classification")],
    Discriminator(_criterion_result_discriminator),
]
```

Each criterion-result class now carries a `result_kind: Literal[...]` field:

| Class                          | `result_kind`     |
| ------------------------------ | ----------------- |
| `CriterionResult`              | `"basic"`         |
| `JudgeCriterionResult`         | `"judge"`         |
| `ClassificationCriterionResult` | `"classification"` |

The serialized `task.json` now carries `"result_kind": "<tag>"` on every entry of
`success_criteria_results`. The discriminator preserves the concrete subclass on
`model_validate_json` reload, so:

```python
reloaded = EvaluationResult.model_validate_json(task_json.read_text())
isinstance(reloaded.success_criteria_results[0], JudgeCriterionResult)  # True
```

…where the pre-fix code silently returned `False` and dropped the
subclass-specific fields (`findings`, `transcript`, `observed_label`, …).

## Legacy `task.json` files (no `result_kind`)

A callable discriminator infers the tag from `criterion_type` for records
written before this field existed:

```python
_JUDGE_CRITERION_TYPES = frozenset({"llm_judge", "agent_judge"})
_CLASSIFICATION_CRITERION_TYPES = frozenset({"classification_match", "skill_triggered"})
```

| Inferred path | When |
| -------------- | ---- |
| `"judge"` | `criterion_type` ∈ `{"llm_judge", "agent_judge"}` |
| `"classification"` | `criterion_type` ∈ `{"classification_match", "skill_triggered"}` |
| `"basic"` | Anything else (safe fallback) |

Explicit `result_kind` always wins over inference, so a forward-compat writer
that needs to downgrade a result shape (e.g. record an `llm_judge` as a base
`CriterionResult`) can do so by emitting `"result_kind": "basic"`.

## Convention for new criterion types

When introducing a criterion whose result class is not the base
`CriterionResult`, add its `criterion_type` value to the matching frozenset in
`models/results.py` in the same PR. The frozensets live in `models/results.py`
(not on each criterion class) so the inference rule is reachable at
deserialization time without importing the checker registry.
