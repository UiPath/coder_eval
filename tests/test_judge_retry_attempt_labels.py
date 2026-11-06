"""Retry evidence stays visible without changing canonical turn targets."""

import json
import re
import socket
import subprocess
from datetime import datetime

import pytest

from coder_eval.criteria import agent_judge, llm_judge, system_one_judge
from coder_eval.criteria.base import CheckContext
from coder_eval.evaluation.judge_context import JudgeContext, JudgeContextBuilder
from coder_eval.models import (
    EvaluationResult,
    FinalStatus,
    LiteLLMRoute,
    LLMJudgeCriterion,
    SandboxConfig,
    TokenUsage,
    TurnRecord,
)
from coder_eval.sandbox import Sandbox


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Synthetic judge tests must not open a network connection or start a subprocess")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def turn(number, output=None, *, crashed=False, prompt=None):
    return TurnRecord(
        iteration=number,
        user_input=prompt or f"QUESTION_{number}",
        agent_output=f"FINAL_{number}" if output is None else output,
        crashed=crashed,
        token_usage=TokenUsage(total_cost_usd=number / 100),
    )


def trajectory():
    return [turn(1), turn(2), turn(3), turn(4, "PARTIAL_4 NEVER_X", crashed=True), turn(4), turn(5)]


def build(records, *, budget=80_000):
    return JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=False,
        include_tool_calls=False,
        include_dialog=True,
        max_file_chars=10_000,
        max_dialog_chars=budget,
    ).build(Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "synthetic-judge"), None, records)


def sections(text):
    return re.findall(
        r"\[([^\]]+)\] USER:\n(.*?)\n\[\1\] AGENT:\n(.*?)"
        r"(?=\n\n\[[^\]]+\] USER:|\n\n(?:Call the submit_verdict|After investigating, call the submit_verdict)|\Z)",
        text,
        re.S,
    )


def canonical(text, number):
    rows = [(user.strip(), agent.strip()) for label, user, agent in sections(text) if label == f"Turn {number}"]
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
def offline_judge(monkeypatch):
    requests = []

    async def completion(*, user, **kwargs):
        requests.append(user)
        rows = sections(user)
        if "GLOBAL_NEVER_X" in user:
            correct = not any("NEVER_X" in output for _, _, output in rows)
        else:
            number = int(re.search(r"TARGET=(\d+)", user).group(1))
            expected = "" if "EXPECT_EMPTY" in user else f"FINAL_{number}"
            correct = canonical(user, number) == (f"QUESTION_{number}", expected)
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "synthetic-verdict",
                                "type": "function",
                                "function": {
                                    "name": "submit_verdict",
                                    "arguments": json.dumps(
                                        {
                                            "score": float(correct),
                                            "rationale": "Synthetic target/evidence check",
                                            "findings": [],
                                        }
                                    ),
                                },
                            }
                        ]
                    }
                }
            ],
        }

    monkeypatch.setattr(llm_judge, "invoke_litellm_judge_async", completion)
    return requests


async def grade(records, prompt):
    criterion = LLMJudgeCriterion(
        type="llm_judge",
        description="Synthetic retry criterion",
        prompt=prompt,
        include_dialog=True,
        include_reference=False,
        include_agent_output=False,
        capture_transcript=True,
        model="openai/synthetic-offline",
    )
    result = await llm_judge.LLMJudgeChecker().check_async(
        criterion,
        Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "synthetic-judge"),
        turn_records=records,
        context=CheckContext(route=LiteLLMRoute()),
    )
    assert result.error is None
    return result


@pytest.mark.parametrize("number", range(1, 6))
async def test_public_checker_targets_latest_record_after_retry(number, offline_judge):
    result = await grade(trajectory(), f"TARGET={number}")
    assert result.score == 1.0


async def test_global_criterion_retains_failed_attempt_evidence(offline_judge):
    result = await grade(trajectory(), "GLOBAL_NEVER_X")
    assert result.score == 0.0
    assert "PARTIAL_4 NEVER_X" in offline_judge[-1]
    assert "Earlier failed attempt evidence for turn 4" in offline_judge[-1]


@pytest.mark.parametrize("placeholder_crashed", [False, True])
async def test_partial_before_empty_bookkeeping_is_visible_without_becoming_final(placeholder_crashed, offline_judge):
    records = [
        turn(1),
        turn(2),
        turn(3),
        turn(4, "MEANINGFUL_PARTIAL NEVER_X", crashed=True),
        turn(4, "", crashed=placeholder_crashed),
    ]
    if placeholder_crashed:
        message = llm_judge._render_user_message("Synthetic", build(records))
        assert not any(label == "Turn 4" for label, _, _ in sections(message))
        assert "[Failed attempt evidence for turn 4]" in message
    else:
        result = await grade(records, "TARGET=4 EXPECT_EMPTY")
        assert result.score == 1.0
        message = offline_judge[-1]
    assert "MEANINGFUL_PARTIAL NEVER_X" in message
    assert "Earlier failed attempt evidence for turn 4" in message


async def test_completed_empty_response_is_not_replaced_with_prior_partial(offline_judge):
    result = await grade([*trajectory()[:4], turn(4, ""), turn(5)], "TARGET=4 EXPECT_EMPTY")
    assert result.score == 1.0


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_repeated_failures_and_distinct_same_text_turns(renderer):
    records = [
        turn(1, "FIRST_FAILURE", crashed=True, prompt="same prompt"),
        turn(1, "SECOND_FAILURE", crashed=True, prompt="same prompt"),
        turn(1, prompt="same prompt"),
        turn(2, prompt="same prompt"),
    ]
    context = build(records)
    message = renderer("Synthetic target", context)
    assert canonical(message, 1) == ("same prompt", "FINAL_1")
    assert canonical(message, 2) == ("same prompt", "FINAL_2")
    assert "FIRST_FAILURE" in message and "SECOND_FAILURE" in message
    assert len(sections(message)) == 4


def test_system_one_state_uses_turn_numbers_and_retains_earlier_attempts():
    state = system_one_judge._build_state("Synthetic", build(trajectory()), 10_000)
    rows = state["dialog"]
    assert [row["turn"] for row in rows] == [1, 2, 3, 4, 4, 5]
    assert rows[3]["earlier_attempt_evidence"] is True
    assert rows[4]["earlier_attempt_evidence"] is False
    assert rows[3]["agent"] == "PARTIAL_4 NEVER_X" and rows[4]["agent"] == "FINAL_4"


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_truncation_does_not_promote_failed_partial_to_canonical(renderer):
    records = [turn(1), turn(2, "PARTIAL_2", crashed=True), turn(2)]
    budget = sum(len(row.user_input) + len(row.agent_output) for row in records[:2]) + 1
    context = build(records, budget=budget)
    message = renderer("Synthetic", context)
    assert len(context.dialog) == 2 and context.degraded_notes
    assert "PARTIAL_2" in message and "FINAL_2" not in message
    assert not any(label == "Turn 2" for label, _, _ in sections(message))


def test_system_one_truncation_does_not_promote_prior_attempt():
    records = [turn(1), turn(2, "PARTIAL_2", crashed=True), turn(2)]
    budget = sum(len(row.user_input) + len(row.agent_output) for row in records[:2]) + 1
    rows = system_one_judge._build_state("Synthetic", build(records, budget=budget), 10_000)["dialog"]
    assert rows[1]["turn"] == 2 and rows[1]["earlier_attempt_evidence"] is True


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_no_retry_and_manual_contexts_remain_byte_identical(renderer):
    records = [turn(1), turn(2), turn(3)]
    context = build(records)
    manual = JudgeContext(dialog=[(row.user_input, row.agent_output) for row in records])
    assert renderer("Synthetic", context) == renderer("Synthetic", manual)
    assert renderer("Synthetic", JudgeContext()) == renderer("Synthetic", JudgeContext(dialog=[]))


def test_system_one_no_retry_state_remains_identical():
    records = [turn(1), turn(2), turn(3)]
    manual = JudgeContext(dialog=[(row.user_input, row.agent_output) for row in records])
    assert system_one_judge._build_state("Synthetic", build(records), 10_000) == system_one_judge._build_state(
        "Synthetic", manual, 10_000
    )


def test_raw_attempts_and_all_costs_are_unchanged_and_persisted(tmp_path):
    records = trajectory()
    result = EvaluationResult(
        task_id="synthetic",
        task_description="Input trajectory fixture",
        variant_id="fixture",
        agent_type="none",
        started_at=datetime.now(),
        final_status=FinalStatus.NOT_GRADED,
        iteration_count=5,
        iterations=records,
    )
    before = result.model_dump_json()
    build(result.iterations)
    assert result.model_dump_json() == before
    path = tmp_path / "task.json"
    path.write_text(before, encoding="utf-8")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row["iteration"] for row in saved["iterations"]] == [1, 2, 3, 4, 4, 5]
    assert saved["iterations"][3]["crashed"] is True
    assert sum(row["token_usage"]["total_cost_usd"] for row in saved["iterations"]) == pytest.approx(0.19)
    assert saved["iterations"][3]["agent_output"] == "PARTIAL_4 NEVER_X"


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_manual_context_does_not_infer_retries_from_same_text(renderer):
    context = JudgeContext(dialog=[("same prompt", "first"), ("same prompt", "second")])
    message = renderer("Synthetic", context)
    assert canonical(message, 1) == ("same prompt", "first")
    assert canonical(message, 2) == ("same prompt", "second")


def test_system_one_manual_context_keeps_original_shape():
    context = JudgeContext(dialog=[("same prompt", "first"), ("same prompt", "second")])
    rows = system_one_judge._build_state("Synthetic", context, 10_000)["dialog"]
    assert rows == [
        {"turn": 1, "user": "same prompt", "agent": "first"},
        {"turn": 2, "user": "same prompt", "agent": "second"},
    ]


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_failed_attempt_labels_come_from_crashed_flag(renderer):
    records = [turn(1, "EARLIER_RETURNED"), turn(1, "ACTUAL_CRASH", crashed=True), turn(1)]
    message = renderer("Synthetic", build(records))
    assert "[Earlier attempt evidence for turn 1]" in message
    assert "[Earlier failed attempt evidence for turn 1]" in message
    assert canonical(message, 1) == ("QUESTION_1", "FINAL_1")
    assert all(text in message for text in ["EARLIER_RETURNED", "ACTUAL_CRASH", "FINAL_1"])


def test_system_one_preserves_explicit_crashed_attempt_metadata():
    records = [turn(1, "EARLIER_RETURNED"), turn(1, "ACTUAL_CRASH", crashed=True), turn(1)]
    rows = system_one_judge._build_state("Synthetic", build(records), 10_000)["dialog"]
    assert [row["turn"] for row in rows] == [1, 1, 1]
    assert [row["earlier_attempt_evidence"] for row in rows] == [True, True, False]
    assert [row["crashed"] for row in rows] == [False, True, False]


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_latest_failed_attempt_is_not_presented_as_final_answer(renderer):
    records = [turn(1), turn(2, "FIRST_FAILURE", crashed=True), turn(2, "LATEST_FAILURE", crashed=True)]
    message = renderer("Synthetic", build(records))
    assert not any(label == "Turn 2" for label, _, _ in sections(message))
    assert "[Earlier failed attempt evidence for turn 2]" in message
    assert "[Failed attempt evidence for turn 2]" in message
    assert "FIRST_FAILURE" in message and "LATEST_FAILURE" in message


def test_system_one_latest_failed_attempt_stays_failed():
    records = [turn(1), turn(2, "FIRST_FAILURE", crashed=True), turn(2, "LATEST_FAILURE", crashed=True)]
    rows = system_one_judge._build_state("Synthetic", build(records), 10_000)["dialog"]
    assert rows[-1]["turn"] == 2
    assert rows[-1]["earlier_attempt_evidence"] is False
    assert rows[-1]["crashed"] is True
    assert rows[-1]["agent"] == "LATEST_FAILURE"


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_single_failed_record_is_explicit_without_duplicate_iteration(renderer):
    message = renderer("Synthetic", build([turn(1, "ONLY_FAILURE", crashed=True)]))
    assert "[Failed attempt evidence for turn 1]" in message
    assert "ONLY_FAILURE" in message
    assert not any(label == "Turn 1" for label, _, _ in sections(message))


def test_system_one_single_failed_record_is_explicit():
    rows = system_one_judge._build_state("Synthetic", build([turn(1, "ONLY_FAILURE", crashed=True)]), 10_000)["dialog"]
    assert rows == [
        {"turn": 1, "user": "QUESTION_1", "agent": "ONLY_FAILURE", "earlier_attempt_evidence": False, "crashed": True}
    ]
