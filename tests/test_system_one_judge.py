"""Tests for the system_one_judge success criterion.

Two halves, matching the implementation: the pure rubric reduction
(``evaluation/system_one_scoring.py``) and the checker's plumbing around it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from coder_eval.criteria import init_criteria
from coder_eval.errors import JudgeInfrastructureError
from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.evaluation.system_one_scoring import build_questions_payload, reduce_answers
from coder_eval.models import (
    ChoiceQuestion,
    CommandTelemetry,
    NoulQuestion,
    SandboxConfig,
    ScoreQuestion,
    SystemOneJudgeCriterion,
    TurnRecord,
)
from coder_eval.sandbox import Sandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    sb = Sandbox(SandboxConfig(driver="tempdir"), task_id="system_one_test")
    sb.sandbox_dir = tmp_path
    return sb


def _response(answers: dict[str, Any], usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {"model": "jev-latest", "answers": answers, "usage": usage or {"input_tokens": 100, "output_tokens": 0}}


async def _run(criterion: SystemOneJudgeCriterion, sandbox: Sandbox, response: dict[str, Any]):
    init_criteria()
    checker = SuccessChecker(sandbox, init_registry=False)
    with patch(
        "coder_eval.criteria.system_one_judge.invoke_system_one_async",
        new=AsyncMock(return_value=response),
    ) as mock:
        results = await checker.check_all_async([criterion])
    return results[0], mock


def _noul_criterion(**kwargs: Any) -> SystemOneJudgeCriterion:
    return SystemOneJudgeCriterion(
        description="d",
        questions={"correct": NoulQuestion(instructions="Is it correct?")},
        **kwargs,
    )


# --- the pure reduction -------------------------------------------------------


def test_noul_expected_scoring_uses_the_probability_directly():
    questions = {"correct": NoulQuestion(instructions="q")}
    verdict = reduce_answers(questions, {"correct": {"noul": 0.8}}, mode="expected")
    assert verdict.score == pytest.approx(0.8)


def test_noul_expected_false_inverts_the_probability():
    questions = {"clean": NoulQuestion(instructions="q", expected=False)}
    verdict = reduce_answers(questions, {"clean": {"noul": 0.8}}, mode="expected")
    assert verdict.score == pytest.approx(0.2)


def test_noul_argmax_collapses_to_the_decision():
    questions = {"correct": NoulQuestion(instructions="q")}
    assert reduce_answers(questions, {"correct": {"noul": 0.8}}, mode="argmax").score == 1.0
    assert reduce_answers(questions, {"correct": {"noul": 0.4}}, mode="argmax").score == 0.0


def test_choice_expected_scoring_weights_the_whole_distribution():
    questions = {
        "quality": ChoiceQuestion(
            instructions="q",
            criteria={"good": None, "bad": None},
            expected="good",
        )
    }
    answer = {"choice": "good", "probabilities": {"good": 0.7, "bad": 0.3}, "confidence": 0.5}
    assert reduce_answers(questions, {"quality": answer}, mode="expected").score == pytest.approx(0.7)
    assert reduce_answers(questions, {"quality": answer}, mode="argmax").score == 1.0


def test_choice_values_give_partial_credit_per_option():
    questions = {
        "quality": ChoiceQuestion(
            instructions="q",
            criteria={"good": None, "ok": None, "bad": None},
            values={"good": 1.0, "ok": 0.5},
        )
    }
    answer = {"choice": "ok", "probabilities": {"good": 0.2, "ok": 0.5, "bad": 0.3}}
    # 0.2*1.0 + 0.5*0.5 + 0.3*0.0
    assert reduce_answers(questions, {"quality": answer}, mode="expected").score == pytest.approx(0.45)
    assert reduce_answers(questions, {"quality": answer}, mode="argmax").score == pytest.approx(0.5)


def test_score_levels_default_to_an_even_ramp():
    questions = {"depth": ScoreQuestion(instructions="q", criteria=["none", "some", "full"])}
    answer = {"score": 1.0, "probabilities": {"0": 0.0, "1": 1.0, "2": 0.0}}
    assert reduce_answers(questions, {"depth": answer}, mode="expected").score == pytest.approx(0.5)


def test_score_values_override_the_ramp():
    questions = {"depth": ScoreQuestion(instructions="q", criteria=["none", "some", "full"], values=[1.0, 0.25, 0.0])}
    answer = {"score": 0.0, "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0}}
    assert reduce_answers(questions, {"depth": answer}, mode="expected").score == pytest.approx(1.0)


def test_weights_bias_the_mean():
    questions = {
        "a": NoulQuestion(instructions="q", weight=3.0),
        "b": NoulQuestion(instructions="q", weight=1.0),
    }
    verdict = reduce_answers(questions, {"a": {"noul": 1.0}, "b": {"noul": 0.0}}, mode="expected")
    assert verdict.score == pytest.approx(0.75)


def test_missing_and_malformed_answers_score_zero_at_full_weight():
    questions = {
        "answered": NoulQuestion(instructions="q"),
        "absent": NoulQuestion(instructions="q"),
        "wrong_shape": NoulQuestion(instructions="q"),
    }
    verdict = reduce_answers(
        questions,
        {"answered": {"noul": 1.0}, "wrong_shape": {"choice": "nope"}},
        mode="expected",
    )
    assert verdict.score == pytest.approx(1 / 3)
    assert "absent: no answer returned" in "\n".join(verdict.findings)
    assert "wrong_shape: malformed answer" in "\n".join(verdict.findings)


def test_unusable_distribution_falls_back_to_the_point_answer():
    questions = {"quality": ChoiceQuestion(instructions="q", criteria={"good": None, "bad": None}, expected="good")}
    answer = {"choice": "good", "probabilities": {"good": "NaN", "bad": 0.0}}
    assert reduce_answers(questions, {"quality": answer}, mode="expected").score == 1.0


def test_findings_record_the_arithmetic_per_question():
    questions = {"correct": NoulQuestion(instructions="q", weight=2.0)}
    verdict = reduce_answers(questions, {"correct": {"noul": 0.6}}, mode="expected")
    assert verdict.findings == ["correct: P(yes)=0.60, expected=true -> 0.60 (weight 2)"]


def test_payload_carries_only_the_wire_fields():
    questions = {
        "a": NoulQuestion(instructions="ia", weight=5.0, expected=False),
        "b": ChoiceQuestion(instructions="ib", criteria={"x": "desc"}, expected="x"),
        "c": ScoreQuestion(instructions="ic", criteria=["lo", "hi"], values=[0.0, 1.0]),
    }
    assert build_questions_payload(questions) == {
        "a": {"type": "noul", "instructions": "ia"},
        "b": {"type": "choice", "instructions": "ib", "criteria": {"x": "desc"}},
        "c": {"type": "score", "instructions": "ic", "criteria": ["lo", "hi"]},
    }


# --- non-finite wire values ---------------------------------------------------
#
# json.loads accepts the NaN / Infinity tokens, so these arrive through an
# ordinary 200. NaN is unordered, so a naive min/max clamp maps it to FULL
# credit — the worst possible direction for a grader to fail in.


@pytest.mark.parametrize("hostile", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_noul_scores_zero_not_full_credit(hostile: float):
    questions = {"correct": NoulQuestion(instructions="q")}
    verdict = reduce_answers(questions, {"correct": {"noul": hostile}}, mode="expected")
    assert verdict.score == 0.0
    assert "malformed answer" in verdict.findings[0]


@pytest.mark.parametrize("hostile", [float("nan"), float("inf")])
def test_non_finite_score_answer_scores_zero(hostile: float):
    questions = {"depth": ScoreQuestion(instructions="q", criteria=["none", "some", "full"])}
    assert reduce_answers(questions, {"depth": {"score": hostile}}, mode="expected").score == 0.0


def test_non_finite_probability_falls_back_instead_of_crediting():
    """A NaN in the distribution must not become a weighted mean of NaN."""
    questions = {"quality": ChoiceQuestion(instructions="q", criteria=["good", "bad"], expected="good")}
    answer = {"choice": "bad", "probabilities": {"good": float("nan"), "bad": 1.0}}
    assert reduce_answers(questions, {"quality": answer}, mode="expected").score == 0.0


def test_question_weight_rejects_non_finite_and_absurd_values():
    for bad in (float("inf"), float("nan"), 1e12):
        with pytest.raises(ValueError):
            NoulQuestion(instructions="q", weight=bad)


def test_argmax_noul_does_not_pass_a_coin_flip_in_both_directions():
    """At P(yes)=0.5 agreement is 0.5 whichever way `expected` points."""
    yes = {"a": NoulQuestion(instructions="q")}
    no = {"a": NoulQuestion(instructions="q", expected=False)}
    assert reduce_answers(yes, {"a": {"noul": 0.5}}, mode="argmax").score == 0.0
    assert reduce_answers(no, {"a": {"noul": 0.5}}, mode="argmax").score == 0.0


# --- rubric validation --------------------------------------------------------


def test_choice_accepts_a_bare_list_of_options():
    question = ChoiceQuestion.model_validate(
        {"instructions": "q", "criteria": ["a", "b", "c"], "expected": "c", "weight": 2.0}
    )
    assert question.criteria == {"a": None, "b": None, "c": None}
    assert question.expected == "c"
    assert question.weight == 2.0


def test_bare_list_options_keep_their_declared_order_on_the_wire():
    question = ChoiceQuestion.model_validate({"instructions": "q", "criteria": ["z", "m", "a"], "expected": "a"})
    assert list(build_questions_payload({"q": question})["q"]["criteria"]) == ["z", "m", "a"]


def test_bare_list_options_reject_non_string_entries():
    """YAML's bare `yes`/`no` parse as bools; str() would make them 'True'/'False'."""
    with pytest.raises(ValueError, match="must be strings"):
        ChoiceQuestion.model_validate({"instructions": "q", "criteria": ["yes", True], "expected": "yes"})
    with pytest.raises(ValueError, match="must be strings"):
        ChoiceQuestion.model_validate({"instructions": "q", "criteria": ["a", None], "expected": "a"})


def test_bare_list_options_reject_duplicates():
    with pytest.raises(ValueError, match="duplicate options: b"):
        ChoiceQuestion.model_validate({"instructions": "q", "criteria": ["a", "b", "b"], "expected": "a"})


def test_bare_list_options_still_validate_expected():
    with pytest.raises(ValueError, match="not one of its options"):
        ChoiceQuestion.model_validate({"instructions": "q", "criteria": ["a", "b"], "expected": "c"})


def test_choice_needs_exactly_one_of_expected_or_values():
    with pytest.raises(ValueError, match="exactly one of"):
        ChoiceQuestion(instructions="q", criteria={"a": None})
    with pytest.raises(ValueError, match="exactly one of"):
        ChoiceQuestion(instructions="q", criteria={"a": None}, expected="a", values={"a": 1.0})


def test_choice_expected_must_name_a_declared_option():
    with pytest.raises(ValueError, match="not one of its options"):
        ChoiceQuestion(instructions="q", criteria={"a": None}, expected="b")


def test_score_level_count_is_bounded():
    with pytest.raises(ValueError, match="levels"):
        ScoreQuestion(instructions="q", criteria=["only"])
    with pytest.raises(ValueError, match="levels"):
        ScoreQuestion(instructions="q", criteria=[str(i) for i in range(11)])


def test_score_values_must_match_the_level_count():
    with pytest.raises(ValueError, match="3 levels but 2 values"):
        ScoreQuestion(instructions="q", criteria=["a", "b", "c"], values=[0.0, 1.0])


def test_empty_rubric_is_rejected():
    with pytest.raises(ValueError, match="at least one entry"):
        SystemOneJudgeCriterion(description="d", questions={})


def test_unknown_yaml_key_is_rejected():
    with pytest.raises(ValueError):
        SystemOneJudgeCriterion.model_validate(
            {"description": "d", "questions": {"q": {"type": "noul", "instructions": "i"}}, "temperatur": 0.0}
        )


# --- the checker --------------------------------------------------------------


async def test_checker_scores_from_the_answers(sandbox: Sandbox):
    result, _ = await _run(_noul_criterion(), sandbox, _response({"correct": {"noul": 0.9}}))
    assert result.score == pytest.approx(0.9)
    assert result.error is None
    assert result.criterion_type == "system_one_judge"


async def test_checker_prices_the_call(sandbox: Sandbox):
    result, _ = await _run(_noul_criterion(), sandbox, _response({"correct": {"noul": 1.0}}))
    assert result.token_usage is not None
    assert result.token_usage.uncached_input_tokens == 100
    assert result.token_usage.total_cost_usd == pytest.approx(100 * 0.042 / 1_000_000)


async def test_checker_sends_the_files_and_the_rubric(sandbox: Sandbox, tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")
    criterion = _noul_criterion(files=["main.py"], prompt="Grade the refactor.")
    _, mock = await _run(criterion, sandbox, _response({"correct": {"noul": 1.0}}))

    assert mock.await_args is not None
    kwargs = mock.await_args.kwargs
    assert kwargs["model"] == "jev-latest"
    assert kwargs["state"]["files"]["main.py"] == "print('hi')"
    assert kwargs["state"]["context"] == "Grade the refactor."
    assert kwargs["questions"]["correct"]["type"] == "noul"


async def test_trajectory_and_conversation_are_opt_in(sandbox: Sandbox):
    """include_tool_calls / include_agent_output widen the state the judge reads."""
    turn = TurnRecord(
        iteration=1,
        user_input="write it",
        agent_output="I wrote main.py",
        commands=[
            CommandTelemetry(
                tool_name="Write",
                tool_id="tool_0",
                timestamp=datetime.now(),
                parameters={"file_path": "main.py"},
                result_status="success",
                sequence_number=0,
            )
        ],
    )

    async def _state_for(**flags: bool) -> dict[str, Any]:
        init_criteria()
        checker = SuccessChecker(sandbox, init_registry=False)
        with patch(
            "coder_eval.criteria.system_one_judge.invoke_system_one_async",
            new=AsyncMock(return_value=_response({"correct": {"noul": 1.0}})),
        ) as mock:
            await checker.check_all_async([_noul_criterion(**flags)], turn_records=[turn])
        assert mock.await_args is not None
        return mock.await_args.kwargs["state"]

    off = await _state_for()
    assert "agent_tool_calls" not in off
    assert "agent_output" not in off

    on = await _state_for(include_tool_calls=True, include_agent_output=True)
    assert "Write" in on["agent_tool_calls"]
    assert "I wrote main.py" in on["agent_output"]


async def test_disabled_criterion_makes_no_call(sandbox: Sandbox):
    result, mock = await _run(_noul_criterion(enabled=False), sandbox, _response({}))
    assert result.score == 1.0
    assert result.details == "(skipped: enabled=false)"
    mock.assert_not_awaited()


async def test_transport_failure_escalates(sandbox: Sandbox):
    init_criteria()
    checker = SuccessChecker(sandbox, init_registry=False)
    with (
        patch(
            "coder_eval.criteria.system_one_judge.invoke_system_one_async",
            new=AsyncMock(side_effect=JudgeInfrastructureError("boom")),
        ),
        pytest.raises(JudgeInfrastructureError),
    ):
        await checker.check_all_async([_noul_criterion()])


async def test_transcript_captures_the_rubric_and_the_answers(sandbox: Sandbox):
    result, _ = await _run(_noul_criterion(), sandbox, _response({"correct": {"noul": 0.5}}))
    assert result.transcript is not None
    assert '"noul": 0.5' in result.transcript.raw_verdict
    assert '"type": "noul"' in result.transcript.judge_system_prompt


async def test_missing_answers_map_escalates_rather_than_scoring_zero(sandbox: Sandbox):
    """A 200 with no usable `answers` is infrastructure, not a bad agent."""
    init_criteria()
    checker = SuccessChecker(sandbox, init_registry=False)
    for broken in ({"model": "jev-latest", "usage": {}}, {"answers": "nope", "usage": {}}):
        with (
            patch(
                "coder_eval.criteria.system_one_judge.invoke_system_one_async",
                new=AsyncMock(return_value=broken),
            ),
            pytest.raises(JudgeInfrastructureError, match="no usable 'answers'"),
        ):
            await checker.check_all_async([_noul_criterion()])


async def test_served_model_is_recorded_over_the_requested_alias(sandbox: Sandbox):
    """`jev-latest` floats, so the grade must record the version actually served."""
    response = _response({"correct": {"noul": 1.0}})
    response["model"] = "jev-1.13.0"
    result, _ = await _run(_noul_criterion(), sandbox, response)
    assert result.transcript is not None
    assert '"jev-1.13.0"' in result.transcript.raw_verdict


async def test_reference_is_scrubbed_from_the_transcript_and_findings(sandbox: Sandbox, tmp_path: Path):
    """Leak canary: the reference must not survive into anything we persist.

    The state is persisted as JSON, so the sentinel is re-encoded on the way
    out — a scrub that only matched the raw file text would miss it.
    """
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    sentinel = 'SUPER_SECRET_REFERENCE\n\tdef solve():\n\t    return "42"\n'
    (reference_dir / "solution.py").write_text(sentinel, encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")

    init_criteria()
    checker = SuccessChecker(sandbox, init_registry=False)
    criterion = _noul_criterion(files=["main.py"], include_reference=True)
    with patch(
        "coder_eval.criteria.system_one_judge.invoke_system_one_async",
        new=AsyncMock(return_value=_response({"correct": {"noul": 1.0}})),
    ):
        results = await checker.check_all_async([criterion], reference_dir=reference_dir)

    result = results[0]
    assert result.transcript is not None
    persisted = "\n".join(
        [result.details or "", *result.findings, result.transcript.judge_prompt, result.transcript.raw_verdict]
    )
    assert "SUPER_SECRET_REFERENCE" not in persisted


async def test_transcript_is_dropped_when_not_requested(sandbox: Sandbox):
    result, _ = await _run(_noul_criterion(capture_transcript=False), sandbox, _response({"correct": {"noul": 0.5}}))
    assert result.transcript is None
