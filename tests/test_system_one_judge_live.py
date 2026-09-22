"""Live integration test for the ``system_one_judge`` transport — hits the real
TypeSafe System One endpoint.

The unit tests mock the invoker, so nothing else in the repo checks that the
request we build is one the API accepts, or that the answers we reduce have the
shape we assume. That shape is external and versioned by someone else — the
string level keys in a ``score`` answer's ``probabilities`` are exactly the sort
of thing a mock will happily keep agreeing with after the wire changes.

Requirements: ``TYPESAFE_API_KEY`` in the environment.

Run with: ``pytest -m live``.
"""

from __future__ import annotations

import os

import pytest

from coder_eval.evaluation.judge_system_one import invoke_system_one_async
from coder_eval.evaluation.system_one_scoring import build_questions_payload, reduce_answers
from coder_eval.models import (
    DEFAULT_SYSTEM_ONE_BASE_URL,
    DEFAULT_SYSTEM_ONE_MODEL,
    ChoiceQuestion,
    NoulQuestion,
    ScoreQuestion,
)


_live = pytest.mark.live
_skip_reason = "Live System One test needs TYPESAFE_API_KEY"
pytestmark = [_live, pytest.mark.skipif(not os.getenv("TYPESAFE_API_KEY"), reason=_skip_reason)]


_CODE = (
    "def fetch(url, tries=3):\n"
    "    for attempt in range(tries):\n"
    "        try:\n"
    "            return requests.get(url, timeout=5).json()\n"
    "        except requests.Timeout:\n"
    "            time.sleep(2 ** attempt)\n"
    "    raise RuntimeError('all retries failed')\n"
)


@_live
async def test_every_primitive_round_trips_against_the_real_api():
    questions = {
        "has_retry": NoulQuestion(instructions="Does the code retry on failure?"),
        "style": ChoiceQuestion(
            instructions="What best describes the error handling?",
            criteria={"absent": "no handling at all", "broad": "a bare except", "targeted": "specific exceptions"},
            values={"targeted": 1.0, "broad": 0.4},
        ),
        "readability": ScoreQuestion(
            instructions="How readable is this function?",
            criteria=["cryptic", "workable", "clear"],
        ),
    }

    response = await invoke_system_one_async(
        base_url=DEFAULT_SYSTEM_ONE_BASE_URL,
        api_key=os.environ["TYPESAFE_API_KEY"],
        model=DEFAULT_SYSTEM_ONE_MODEL,
        state={"files": {"client.py": _CODE}},
        questions=build_questions_payload(questions),
    )

    answers = response["answers"]
    assert set(answers) == set(questions), "answers come back under the rubric's own keys"
    assert isinstance(answers["has_retry"]["noul"], float)
    assert answers["style"]["choice"] in questions["style"].criteria
    # Level keys are STRINGS ("0", "1", ...) — the reduction indexes on that.
    assert set(answers["readability"]["probabilities"]) == {"0", "1", "2"}
    assert response["usage"]["input_tokens"] > 0

    verdict = reduce_answers(questions, answers, mode="expected")
    assert 0.0 <= verdict.score <= 1.0
    assert len(verdict.findings) == len(questions)
    assert not any("malformed" in f or "no answer" in f for f in verdict.findings)
