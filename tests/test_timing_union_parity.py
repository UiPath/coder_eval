"""The Python and TypeScript tool-execution unions must agree.

``coder_eval.timing.busy_ms`` subtracts tool time from an agent's
generation window; ``evalboard/lib/timing.ts::busyMs`` subtracts tool time from a
task's wall clock to produce the task page's ``Unaccounted`` residual. They
answer the same question about the same ``task.json``, so a divergence is not a
style difference — it is the harness and the evalboard reporting two different
tool totals for one run.

Neither implementation owns the numbers: ``tests/_fixtures/timing_union_cases.json``
does, and both suites replay it. The TypeScript half lives in
``evalboard/lib/__tests__/timing-union-parity.test.ts``; the coverage test below
fails if one suite quietly stops reading the file.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from coder_eval.models import CommandTelemetry
from coder_eval.streaming.collector import main_thread_tool_spans
from coder_eval.timing import busy_ms, union_ms


_FIXTURE = Path(__file__).parent / "_fixtures" / "timing_union_cases.json"
_TS_TEST = Path(__file__).parents[1] / "evalboard" / "lib" / "__tests__" / "timing-union-parity.test.ts"
_BASE = datetime(2026, 1, 1, 12, 0, 0)

_CORPUS = json.loads(_FIXTURE.read_text())
_CASES = _CORPUS["cases"]
_UNION_CASES = _CORPUS["union_cases"]
_UNBOUNDED_CASES = _CORPUS["unbounded_cases"]


def _at(offset_ms: float) -> datetime:
    return _BASE + timedelta(milliseconds=offset_ms)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_busy_ms_matches_the_shared_corpus(case: dict) -> None:
    lo, hi = case["window"]
    spans = [(_at(s), _at(e)) for s, e in case["spans"]]
    assert busy_ms(spans, _at(lo), _at(hi)) == pytest.approx(case["expected_ms"])


@pytest.mark.parametrize("case", _UNION_CASES, ids=[c["name"] for c in _UNION_CASES])
def test_union_ms_matches_the_shared_corpus(case: dict) -> None:
    """The same union, with the extent derived rather than handed in.

    ``union_ms`` is what the golden sensor and the live residual gate both
    call; the TypeScript side of these cases is ``toolExecutionMs``, which
    derives the extent with its own ``min``/``max`` rather than being given
    one. That derivation is the only part of the union rule the ``cases``
    array above cannot reach.
    """
    spans = [(_at(s), _at(e)) for s, e in case["spans"]]
    assert union_ms(spans) == pytest.approx(case["expected_ms"])


@pytest.mark.parametrize("case", _UNBOUNDED_CASES, ids=[c["name"] for c in _UNBOUNDED_CASES])
def test_an_unbounded_call_contributes_nothing_to_the_union(case: dict) -> None:
    """The POLICY half, replayed through the PRODUCTION selector.

    ``union_ms`` alone cannot pin this: by the time a span list reaches it the
    unbounded calls are already gone. The decision lives one layer up, in
    ``main_thread_tool_spans``'s ``is not None`` filter — so that is what this
    replays, over hand-built ``CommandTelemetry`` rows shaped the way a harness
    records them. The TypeScript twin (``toolExecutionMs``) makes the same
    decision inline, which is why the corpus and not either implementation owns
    the answer.
    """
    commands = [
        CommandTelemetry(
            tool_id=f"bounded-{i}",
            tool_name="Bash",
            timestamp=_at(start),
            execution_started_at=_at(start),
            execution_completed_at=_at(end),
            result_status="success",
        )
        for i, (start, end) in enumerate(case["spans"])
    ]
    commands += [
        # Timed, never bounded: exactly the codex `Bash` shape, and the shape
        # the out-of-tree delegate-sdk still reports.
        CommandTelemetry(
            tool_id=f"unbounded-{i}",
            tool_name="Bash",
            timestamp=_BASE,
            duration_ms=duration,
            result_status="success",
        )
        for i, duration in enumerate(case["unbounded_ms"])
    ]
    spans = main_thread_tool_spans([], commands)
    assert union_ms(spans) == pytest.approx(case["expected_ms"])


def test_the_typescript_half_replays_the_same_file() -> None:
    """A parity corpus only one side reads is not a parity corpus.

    Asserted on the relative path so moving or renaming the fixture has to
    touch both suites in the same change.
    """
    assert _TS_TEST.is_file(), f"missing TypeScript parity test at {_TS_TEST}"
    source = _TS_TEST.read_text()
    assert "timing_union_cases.json" in source
    # The TS side must exercise every case, not a hand-picked subset — it reads
    # the array rather than restating it.
    assert re.search(r"\.cases\b", source), "the TS test must iterate the corpus, not inline cases"
    assert re.search(r"\.union_cases\b", source), (
        "the TS test must also iterate `union_cases`, the half that pins toolExecutionMs's "
        "own min/max extent against union_ms's"
    )
    assert re.search(r"\.unbounded_cases\b", source), (
        "the TS test must also iterate `unbounded_cases`, the half that pins the POLICY: a "
        "call the harness timed but did not bound contributes nothing on either side"
    )
