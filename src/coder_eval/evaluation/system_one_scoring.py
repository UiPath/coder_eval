"""Rubric <-> wire translation and answer reduction for the System One judge.

Two pure halves, no I/O:

* ``build_questions_payload`` renders the authored rubric into the ``questions``
  map the API expects (only the wire fields — ``weight``, ``expected`` and
  ``values`` are scoring knobs the model never sees).
* ``reduce_answers`` turns the returned answers back into a ``JudgeVerdict``.

The reduction is where the criterion's score actually comes from. A System One
model reports a calibrated distribution, not a grade, so the mapping from
distribution to [0.0, 1.0] is the rubric author's, declared once per question
and applied here — deterministically, so the same answers always grade the same.

Rationale: .claude/notes/contracts.md § System One rubric scoring
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal, TypeGuard

from coder_eval.models import (
    ChoiceQuestion,
    JudgeVerdict,
    NoulQuestion,
    ScoreQuestion,
    SystemOneQuestion,
)


ScoringMode = Literal["expected", "argmax"]
"""``"expected"`` weights every outcome by its probability; ``"argmax"`` reads only the top answer."""


def build_questions_payload(questions: Mapping[str, SystemOneQuestion]) -> dict[str, dict[str, Any]]:
    """Render the rubric into the API's ``questions`` map."""
    payload: dict[str, dict[str, Any]] = {}
    for qid, question in questions.items():
        body: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
        match question:
            case NoulQuestion():
                if question.criteria:
                    body["criteria"] = question.criteria
            case ChoiceQuestion():
                body["criteria"] = question.criteria
            case ScoreQuestion():
                body["criteria"] = question.criteria
        payload[qid] = body
    return payload


def _is_number(value: Any) -> TypeGuard[int | float]:
    """A usable numeric wire value: not a bool, and FINITE.

    ``json.loads`` accepts the ``NaN`` / ``Infinity`` tokens, so a non-finite
    float reaches us through an ordinary 200 response. It has to be rejected
    here rather than at the clamp: NaN is unordered, so ``min(1.0, nan)`` is
    ``1.0`` and a malformed answer would grade as full credit.
    """
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _expected_value(probabilities: Mapping[str, Any], values: Mapping[str, float]) -> float | None:
    """Probability-weighted mean of ``values`` over ``probabilities``.

    Returns ``None`` when the distribution is unusable (absent, non-numeric,
    non-finite, or summing to zero) so the caller can fall back to the point
    answer instead of silently grading a broken payload.
    """
    total = 0.0
    weighted = 0.0
    for key, raw in probabilities.items():
        if not _is_number(raw):
            return None
        total += float(raw)
        weighted += float(raw) * values.get(key, 0.0)
    if total <= 0.0 or not math.isfinite(total) or not math.isfinite(weighted):
        return None
    return weighted / total


def _clamp(value: float) -> float:
    """Clamp into [0.0, 1.0], mapping a non-finite value to NO credit.

    Rationale: .claude/notes/contracts.md § System One rubric scoring
    """
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


class _QuestionError(Exception):
    """The API's answer for one question does not match the question the rubric asked."""


def _resolve_noul(question: NoulQuestion, answer: Mapping[str, Any], mode: ScoringMode) -> tuple[float, str]:
    raw = answer.get("noul")
    if not _is_number(raw):
        raise _QuestionError(f"expected a finite numeric 'noul', got {raw!r}")
    probability = _clamp(float(raw))
    agreement = probability if question.expected else 1.0 - probability
    # Strictly greater: at P(yes)=0.5 agreement is 0.5 whichever way `expected`
    # points, so `>=` would pass a maximally uncertain answer in both directions.
    value = float(agreement > 0.5) if mode == "argmax" else agreement
    detail = f"P(yes)={probability:.2f}, expected={str(question.expected).lower()}"
    return value, detail


def _resolve_choice(question: ChoiceQuestion, answer: Mapping[str, Any], mode: ScoringMode) -> tuple[float, str]:
    chosen = answer.get("choice")
    if not isinstance(chosen, str):
        raise _QuestionError(f"expected a string 'choice', got {chosen!r}")
    values = {question.expected: 1.0} if question.expected is not None else dict(question.values or {})
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")

    value: float | None = None
    if mode == "expected" and isinstance(probabilities, Mapping):
        value = _expected_value(probabilities, values)
    if value is None:
        value = values.get(chosen, 0.0)
    detail = f"choice={chosen!r}"
    if _is_number(confidence):
        detail += f" (confidence {float(confidence):.2f})"
    return _clamp(value), detail


def _resolve_score(question: ScoreQuestion, answer: Mapping[str, Any], mode: ScoringMode) -> tuple[float, str]:
    raw = answer.get("score")
    if not _is_number(raw):
        raise _QuestionError(f"expected a finite numeric 'score', got {raw!r}")
    levels = question.level_values()
    last = len(levels) - 1
    position = max(0.0, min(float(last), float(raw)))
    # Level indices come back as STRING keys ("0", "1", ...) in `probabilities`.
    by_index = {str(i): v for i, v in enumerate(levels)}
    probabilities = answer.get("probabilities")

    value: float | None = None
    if mode == "expected" and isinstance(probabilities, Mapping):
        value = _expected_value(probabilities, by_index)
    if value is None:
        value = levels[round(position)]
    detail = f"score={position:.2f}/{last}"
    confidence = answer.get("confidence")
    if _is_number(confidence):
        detail += f" (confidence {float(confidence):.2f})"
    return _clamp(value), detail


def reduce_answers(
    questions: Mapping[str, SystemOneQuestion],
    answers: Mapping[str, Any],
    *,
    mode: ScoringMode,
) -> JudgeVerdict:
    """Collapse the API's answers into a weighted-mean verdict.

    Every question contributes at its declared ``weight``. A question the API did
    not answer, or answered with the wrong primitive, contributes 0.0 at full
    weight and says so in ``findings`` — the rubric asked something the grade
    depends on, so dropping it would quietly inflate the score.
    """
    findings: list[str] = []
    weighted_total = 0.0
    weight_total = 0.0
    unanswered = 0

    for qid, question in questions.items():
        answer = answers.get(qid)
        if not isinstance(answer, Mapping):
            findings.append(f"{qid}: no answer returned — scored 0.00")
            unanswered += 1
            weight_total += question.weight
            continue
        try:
            match question:
                case NoulQuestion():
                    value, detail = _resolve_noul(question, answer, mode)
                case ChoiceQuestion():
                    value, detail = _resolve_choice(question, answer, mode)
                case ScoreQuestion():
                    value, detail = _resolve_score(question, answer, mode)
        except _QuestionError as e:
            findings.append(f"{qid}: malformed answer — {e} — scored 0.00")
            unanswered += 1
            weight_total += question.weight
            continue
        findings.append(f"{qid}: {detail} -> {value:.2f} (weight {question.weight:g})")
        weighted_total += value * question.weight
        weight_total += question.weight

    usable_weights = weight_total > 0.0 and math.isfinite(weight_total) and math.isfinite(weighted_total)
    score = weighted_total / weight_total if usable_weights else 0.0
    answered = len(questions) - unanswered
    rationale = f"Weighted mean of {answered}/{len(questions)} rubric answers ({mode} scoring)."
    return JudgeVerdict(score=_clamp(score), rationale=rationale, findings=findings)
