"""Round-trip tests for the ``CriterionResultUnion`` discriminated union.

The list of criterion results on ``EvaluationResult`` is typed as a discriminated
union so subclass-specific fields survive a ``model_dump_json`` →
``model_validate_json`` cycle with the concrete type preserved. Without this,
``isinstance(cr, JudgeCriterionResult)`` silently returns ``False`` after reload
and downstream renderers / aggregators silently drop subclass fields.
"""

from __future__ import annotations

from datetime import datetime

from coder_eval.models import (
    ClassificationCriterionResult,
    CriterionResult,
    EvaluationResult,
    JudgeCriterionResult,
    JudgeTranscript,
)
from coder_eval.models.enums import AgentKind, FinalStatus


def _make_eval(results: list[CriterionResult]) -> EvaluationResult:
    return EvaluationResult(
        task_id="t",
        task_description="d",
        agent_type=AgentKind.CLAUDE_CODE,
        started_at=datetime(2026, 5, 12, 0, 0, 0),
        final_status=FinalStatus.SUCCESS,
        iteration_count=1,
        success_criteria_results=results,
    )


def test_judge_isinstance_after_reload() -> None:
    """JudgeCriterionResult subclass type is preserved across JSON round-trip."""
    er = _make_eval(
        [
            JudgeCriterionResult(
                criterion_type="llm_judge",
                description="x",
                score=0.7,
                findings=["finding-a", "finding-b"],
                transcript=JudgeTranscript(
                    raw_verdict='{"score":0.7,"rationale":"r","findings":["f"]}',
                    duration_seconds=1.2,
                ),
            ),
        ]
    )
    reloaded = EvaluationResult.model_validate_json(er.model_dump_json())
    cr = reloaded.success_criteria_results[0]
    assert isinstance(cr, JudgeCriterionResult)
    assert cr.findings == ["finding-a", "finding-b"]
    assert cr.transcript is not None
    assert cr.transcript.raw_verdict.startswith('{"score":0.7')


def test_classification_isinstance_after_reload() -> None:
    er = _make_eval(
        [
            ClassificationCriterionResult(
                criterion_type="classification_match",
                description="c",
                score=1.0,
                observed_label="positive",
                expected_label="positive",
            ),
        ]
    )
    reloaded = EvaluationResult.model_validate_json(er.model_dump_json())
    cr = reloaded.success_criteria_results[0]
    assert isinstance(cr, ClassificationCriterionResult)
    assert cr.observed_label == "positive"
    assert cr.expected_label == "positive"


def test_base_criterion_round_trip() -> None:
    er = _make_eval(
        [
            CriterionResult(criterion_type="file_exists", description="f", score=1.0),
        ]
    )
    reloaded = EvaluationResult.model_validate_json(er.model_dump_json())
    cr = reloaded.success_criteria_results[0]
    assert type(cr) is CriterionResult
    assert cr.criterion_type == "file_exists"
    assert cr.score == 1.0


def test_legacy_task_json_infers_result_kind_from_criterion_type() -> None:
    """Legacy task.json files (no ``result_kind``) infer the subclass via criterion_type."""
    legacy_payload = {
        "task_id": "t",
        "task_description": "d",
        "agent_type": "claude-code",
        "started_at": "2026-05-12T00:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1,
        "success_criteria_results": [
            {"criterion_type": "llm_judge", "description": "x", "score": 0.5, "findings": ["leg"]},
            {
                "criterion_type": "agent_judge",
                "description": "y",
                "score": 0.6,
                "findings": ["aj"],
            },
            {
                "criterion_type": "classification_match",
                "description": "c",
                "score": 1.0,
                "observed_label": "a",
                "expected_label": "a",
            },
            {
                "criterion_type": "skill_triggered",
                "description": "s",
                "score": 0.0,
                "observed_label": "(none)",
                "expected_label": "Skill",
            },
            {"criterion_type": "file_exists", "description": "f", "score": 1.0},
        ],
    }
    er = EvaluationResult.model_validate(legacy_payload)
    assert isinstance(er.success_criteria_results[0], JudgeCriterionResult)
    assert isinstance(er.success_criteria_results[1], JudgeCriterionResult)
    assert isinstance(er.success_criteria_results[2], ClassificationCriterionResult)
    assert isinstance(er.success_criteria_results[3], ClassificationCriterionResult)
    assert type(er.success_criteria_results[4]) is CriterionResult


def test_unknown_criterion_type_falls_to_basic() -> None:
    """An unknown criterion_type falls through to the base CriterionResult."""
    payload = {
        "task_id": "t",
        "task_description": "d",
        "agent_type": "claude-code",
        "started_at": "2026-05-12T00:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1,
        "success_criteria_results": [
            {
                "criterion_type": "future_check",
                "description": "fu",
                "score": 0.5,
                "unknown_field": 42,
            },
        ],
    }
    er = EvaluationResult.model_validate(payload)
    cr = er.success_criteria_results[0]
    assert type(cr) is CriterionResult
    # extra="allow" preserves the unknown subclass field through __pydantic_extra__
    assert cr.model_extra is not None
    assert cr.model_extra.get("unknown_field") == 42


def test_explicit_result_kind_overrides_inference() -> None:
    """Explicit ``result_kind`` wins over ``criterion_type`` inference."""
    payload = {
        "task_id": "t",
        "task_description": "d",
        "agent_type": "claude-code",
        "started_at": "2026-05-12T00:00:00",
        "final_status": "SUCCESS",
        "iteration_count": 1,
        "success_criteria_results": [
            {
                "result_kind": "basic",
                "criterion_type": "llm_judge",
                "description": "x",
                "score": 0.7,
            },
        ],
    }
    er = EvaluationResult.model_validate(payload)
    cr = er.success_criteria_results[0]
    assert type(cr) is CriterionResult


def test_mixed_result_types_in_one_list() -> None:
    """A single EvaluationResult.success_criteria_results list can mix concrete types."""
    er = _make_eval(
        [
            JudgeCriterionResult(criterion_type="llm_judge", description="x", score=0.7, findings=["a"]),
            ClassificationCriterionResult(
                criterion_type="classification_match",
                description="c",
                score=1.0,
                observed_label="positive",
                expected_label="positive",
            ),
            CriterionResult(criterion_type="file_exists", description="f", score=1.0),
        ]
    )
    reloaded = EvaluationResult.model_validate_json(er.model_dump_json())
    types = [type(r).__name__ for r in reloaded.success_criteria_results]
    assert types == ["JudgeCriterionResult", "ClassificationCriterionResult", "CriterionResult"]
