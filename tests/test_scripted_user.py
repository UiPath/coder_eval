"""Unit tests for ``ScriptedUser``: authored turns replayed without a model."""

from __future__ import annotations

import pytest

from coder_eval.simulation import ScriptedUser


TURNS = ["First question.", "  Second, with spaces.\n", "Third question."]


async def test_serves_the_turn_after_the_completed_exchanges():
    user = ScriptedUser(TURNS)

    assert (await user.next_user_message([])).text == "First question."
    assert (await user.next_user_message([("First question.", "answer")])).text == "  Second, with spaces.\n"
    history = [("First question.", "answer"), ("  Second, with spaces.\n", "answer")]
    assert (await user.next_user_message(history)).text == "Third question."


async def test_the_turn_is_an_ordinary_utterance_that_cost_nothing():
    result = await ScriptedUser(TURNS).next_user_message([])

    assert result.raw_text == result.text
    assert result.stop_requested is False
    assert result.input_tokens is None
    assert result.output_tokens is None


async def test_asking_past_the_last_turn_raises():
    history = [(turn, "answer") for turn in TURNS]

    with pytest.raises(IndexError):
        await ScriptedUser(TURNS).next_user_message(history)


async def test_needs_no_simulator_agent_to_start_or_stop():
    user = ScriptedUser(TURNS)

    await user.start()
    await user.stop()

    assert user.model is None
