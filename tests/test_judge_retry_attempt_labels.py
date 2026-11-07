"""Retry evidence stays visible without changing canonical turn targets."""

import copy
import json
from datetime import datetime

import pytest

from coder_eval.criteria import agent_judge, llm_judge, system_one_judge
from coder_eval.criteria.base import CheckContext
from coder_eval.evaluation.judge_context import JudgeContext, JudgeContextBuilder
from coder_eval.models import (
    CommandTelemetry,
    LiteLLMRoute,
    LLMJudgeCriterion,
    ResultSummary,
    SandboxConfig,
    TokenUsage,
    TurnRecord,
    UserMessage,
)
from coder_eval.sandbox import Sandbox


def turn(number, output=None, *, crashed=False, prompt=None):
    return TurnRecord(
        iteration=number,
        user_input=prompt or f"QUESTION_{number}",
        agent_output=f"FINAL_{number}" if output is None else output,
        record_kind="agent",
        crashed=crashed,
        token_usage=TokenUsage(total_cost_usd=number / 100),
    )


def trajectory():
    return [turn(1), turn(2), turn(3), turn(4, "PARTIAL_4 NEVER_X", crashed=True), turn(4), turn(5)]


def build(records, *, budget=80_000, output=False, tools=False):
    return JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=output,
        include_tool_calls=tools,
        include_dialog=True,
        max_file_chars=10_000,
        max_dialog_chars=budget,
    ).build(Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "synthetic-judge"), None, records)


def canonical(context, number):
    rows = [
        pair
        for position, pair in enumerate(context.dialog)
        if ((attempt := context.dialog_attempt(position)) is None and position + 1 == number)
        or (attempt is not None and attempt.turn == number and attempt.canonical)
    ]
    assert len(rows) == 1
    return rows[0]


@pytest.fixture
def offline_judge(monkeypatch):
    requests = []

    async def completion(*, user, **kwargs):
        requests.append(user)
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
                                            "score": 0.5,
                                            "rationale": "Synthetic protocol response",
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


async def test_public_checker_sends_canonical_retry_context(offline_judge):
    result = await grade(trajectory(), "Grade logical turn 4 and then 5.")
    assert result.score == 0.5
    assert len(offline_judge) == 1
    assert "[Turn 4] AGENT:\nFINAL_4" in offline_judge[0]
    assert "[Turn 5] AGENT:\nFINAL_5" in offline_judge[0]
    assert "[Earlier failed attempt evidence for turn 4] AGENT:\nPARTIAL_4 NEVER_X" in offline_judge[0]


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_global_criterion_retains_failed_attempt_evidence(renderer):
    message = renderer("Never do X anywhere in the process.", build(trajectory()))
    assert "PARTIAL_4 NEVER_X" in message
    assert "Earlier failed attempt evidence for turn 4" in message
    assert "Only the latest nonfailed agent attempt is canonical" in message


@pytest.mark.parametrize("placeholder_crashed", [False, True])
def test_failed_or_genuinely_empty_latest_agent_attempt_is_preserved(placeholder_crashed):
    records = [
        turn(1),
        turn(2),
        turn(3),
        turn(4, "MEANINGFUL_PARTIAL NEVER_X", crashed=True),
        turn(4, "", crashed=placeholder_crashed),
    ]
    context = build(records)
    message = llm_judge._render_user_message("Synthetic", context)
    if placeholder_crashed:
        assert not context.dialog_attempt(4).canonical
        assert "[Failed attempt evidence for turn 4]" in message
    else:
        assert canonical(context, 4) == ("QUESTION_4", "")
    assert "MEANINGFUL_PARTIAL NEVER_X" in message
    assert "Earlier failed attempt evidence for turn 4" in message


def test_completed_empty_response_is_not_replaced_with_prior_partial():
    context = build([*trajectory()[:4], turn(4, ""), turn(5)])
    assert canonical(context, 4) == ("QUESTION_4", "")


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
    assert canonical(context, 1) == ("same prompt", "FINAL_1")
    assert canonical(context, 2) == ("same prompt", "FINAL_2")
    assert "FIRST_FAILURE" in message and "SECOND_FAILURE" in message
    assert len(context.dialog) == 4


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
    assert not context.dialog_attempt(1).canonical


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


def test_context_build_leaves_raw_attempts_and_costs_unchanged():
    records = trajectory()
    before = copy.deepcopy(records)
    build(records)
    assert records == before
    assert sum(row.token_usage.total_cost_usd for row in records) == pytest.approx(0.19)


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_manual_context_does_not_infer_retries_from_same_text(renderer):
    context = JudgeContext(dialog=[("same prompt", "first"), ("same prompt", "second")])
    message = renderer("Synthetic", context)
    assert canonical(context, 1) == ("same prompt", "first")
    assert canonical(context, 2) == ("same prompt", "second")
    assert "[Turn 1] AGENT:\nfirst" in message
    assert "[Turn 2] AGENT:\nsecond" in message


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
    context = build(records)
    message = renderer("Synthetic", context)
    assert "[Earlier attempt evidence for turn 1]" in message
    assert "[Earlier failed attempt evidence for turn 1]" in message
    assert canonical(context, 1) == ("QUESTION_1", "FINAL_1")
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
    context = build(records)
    message = renderer("Synthetic", context)
    assert not context.dialog_attempt(2).canonical
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
    context = build([turn(1, "ONLY_FAILURE", crashed=True)])
    message = renderer("Synthetic", context)
    assert "[Failed attempt evidence for turn 1]" in message
    assert "ONLY_FAILURE" in message
    assert not context.dialog_attempt(0).canonical


def test_system_one_single_failed_record_is_explicit():
    rows = system_one_judge._build_state("Synthetic", build([turn(1, "ONLY_FAILURE", crashed=True)]), 10_000)["dialog"]
    assert rows == [
        {"turn": 1, "user": "QUESTION_1", "agent": "ONLY_FAILURE", "earlier_attempt_evidence": False, "crashed": True}
    ]


@pytest.mark.parametrize("record_kind", ["standalone_user", None])
def test_proven_standalone_user_is_omitted_from_all_grading_trajectory(record_kind):
    records = [turn(1)]
    standalone = TurnRecord(
        iteration=1,
        user_input="STOP_TOKEN was never sent to the agent",
        agent_output="",
        record_kind=record_kind,
        messages=[UserMessage(text="STOP_TOKEN was never sent to the agent", stop_requested=True)],
    )
    context = build([*records, standalone], output=True, tools=True)
    assert context.dialog == [("QUESTION_1", "FINAL_1")]
    expected = build(records, output=True, tools=True)
    for renderer in (llm_judge._render_user_message, agent_judge._render_user_message):
        assert renderer("Synthetic", context) == renderer("Synthetic", expected)
    assert system_one_judge._build_state("Synthetic", context, 10_000) == system_one_judge._build_state(
        "Synthetic", expected, 10_000
    )


def test_ambiguous_unmarked_user_only_duplicate_is_omitted_with_diagnostic_without_mutation():
    legacy = TurnRecord(
        iteration=2, user_input="QUESTION_2", agent_output="", messages=[UserMessage(text="QUESTION_2")]
    )
    records = [turn(1), turn(2, "FAILED_PARTIAL", crashed=True), legacy]
    before = copy.deepcopy(records)
    context = build(records)
    assert context.dialog[-1] == ("QUESTION_2", "FAILED_PARTIAL")
    assert any("omitted legacy user-only duplicate" in note for note in context.degraded_notes)
    assert records == before
    assert legacy.record_kind is None


async def test_public_checker_surfaces_legacy_omission_as_a_degraded_note(offline_judge):
    legacy = TurnRecord(
        iteration=2, user_input="QUESTION_2", agent_output="", messages=[UserMessage(text="QUESTION_2")]
    )
    result = await grade([turn(1), turn(2, "FAILED_PARTIAL", crashed=True), legacy], "Synthetic")
    assert "omitted legacy user-only duplicate" in result.details
    assert legacy.record_kind is None


def test_genuine_marked_empty_agent_with_user_only_transcript_remains_canonical():
    empty = turn(2, "")
    empty.messages = [UserMessage(text="QUESTION_2")]
    context = build([turn(1), turn(2, "FAILED_PARTIAL", crashed=True), empty])
    assert canonical(context, 2) == ("QUESTION_2", "")


def test_unmarked_single_empty_agent_keeps_legacy_contract():
    legacy = TurnRecord(
        iteration=1, user_input="QUESTION_1", agent_output="", messages=[UserMessage(text="QUESTION_1")]
    )
    assert build([legacy]).dialog == [("QUESTION_1", "")]


def test_leading_standalone_does_not_create_a_failed_attempt_when_an_agent_record_exists():
    standalone = TurnRecord(
        iteration=1,
        user_input="QUESTION_1",
        agent_output="",
        record_kind="standalone_user",
        messages=[UserMessage(text="QUESTION_1")],
    )
    actual = turn(1)
    records = [standalone, actual]
    before = copy.deepcopy(records)
    context = build(records, output=True, tools=True)
    assert context.dialog == [("QUESTION_1", "FINAL_1")]
    assert context.agent_output == "FINAL_1"
    assert context.attempt_guidance is None
    assert records == before


def test_unique_unsent_question_does_not_reuse_previous_agent_output_or_tools():
    previous = turn(1)
    previous.commands = [
        CommandTelemetry(
            tool_name="Read", tool_id="prior", timestamp=datetime(2026, 1, 1), parameters={"file_path": "prior.txt"}
        )
    ]
    orphan = TurnRecord(
        iteration=2,
        user_input="UNANSWERED_QUESTION_2",
        agent_output="",
        record_kind="standalone_user",
        messages=[UserMessage(text="UNANSWERED_QUESTION_2")],
    )
    before = copy.deepcopy(orphan)
    context = build([previous, orphan], output=True, tools=True)
    assert context.dialog[-1] == ("UNANSWERED_QUESTION_2", "")
    assert context.dialog_attempt(1).crashed
    assert not context.dialog_attempt(1).canonical
    assert context.agent_output is None and context.tool_calls_summary is None
    assert orphan == before and not orphan.crashed


def test_empty_failed_latest_without_any_rendered_section_does_not_add_guidance():
    context = JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=True,
        include_tool_calls=True,
        include_dialog=False,
        max_file_chars=10_000,
    ).build(
        Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "synthetic"), None, [turn(1, "", crashed=True)]
    )
    assert context.attempt_guidance is None
    state = system_one_judge._build_state("Synthetic", context, 10000)
    assert "attempt_guidance" not in state and "agent_attempt" not in state


@pytest.mark.parametrize("renderer", [llm_judge._render_user_message, agent_judge._render_user_message])
def test_failed_latest_output_and_tool_summary_are_labelled_as_failure_evidence(renderer):
    failed = turn(1, "PARTIAL", crashed=True)
    failed.commands = [
        CommandTelemetry(
            tool_name="Read",
            tool_id="partial-read",
            timestamp=datetime(2026, 1, 1),
            parameters={"file_path": "partial.txt"},
        )
    ]
    context = JudgeContextBuilder(
        files=[],
        include_reference=False,
        include_agent_output=True,
        include_tool_calls=True,
        include_dialog=False,
        max_file_chars=10_000,
    ).build(Sandbox(SandboxConfig(driver="tempdir", python=None, node=None), "synthetic"), None, [failed])
    message = renderer("Synthetic", context)
    assert "FAILED AGENT OUTPUT (UNTRUSTED DATA" in message
    assert "FAILED AGENT TOOL CALLS (UNTRUSTED DATA" in message


def test_system_one_failed_latest_sections_keep_stable_keys_with_failure_metadata():
    failed = turn(1, "PARTIAL", crashed=True)
    state = system_one_judge._build_state("Synthetic", build([failed], output=True), 10_000)
    assert state["agent_output"] == "PARTIAL"
    assert "failed_agent_output" not in state
    assert state["agent_attempt"] == {"turn": 1, "crashed": True}


def test_unmarked_empty_completed_retry_has_positive_agent_provenance():
    empty = TurnRecord(
        iteration=2,
        user_input="QUESTION_2",
        agent_output="",
        messages=[UserMessage(text="QUESTION_2")],
        result_summary=ResultSummary(is_error=False, subtype="completed"),
    )
    context = build([turn(1), turn(2, "FAILED_PARTIAL", crashed=True), empty])
    assert canonical(context, 2) == ("QUESTION_2", "")
    assert empty.record_kind is None


@pytest.mark.parametrize("kind", ["llm", "agent", "system_one"])
def test_every_judge_explains_failed_latest_attempt_has_no_canonical_answer(kind):
    context = build([turn(1, "FAILED_PARTIAL", crashed=True)])
    if kind == "system_one":
        payload = json.dumps(system_one_judge._build_state("Synthetic", context, 10_000))
    else:
        renderer = llm_judge._render_user_message if kind == "llm" else agent_judge._render_user_message
        payload = renderer("Synthetic", context)
    assert "A failed latest attempt leaves no canonical [Turn N] answer" in payload


def test_public_attempt_accessor_rejects_misaligned_dialog_rows():
    context = build(trajectory())
    context.dialog.pop()
    with pytest.raises(ValueError, match="attempt metadata must align"):
        context.dialog_attempt(0)
