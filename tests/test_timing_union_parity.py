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

from coder_eval.timing import busy_ms


_FIXTURE = Path(__file__).parent / "_fixtures" / "timing_union_cases.json"
_TS_TEST = Path(__file__).parents[1] / "evalboard" / "lib" / "__tests__" / "timing-union-parity.test.ts"
_BASE = datetime(2026, 1, 1, 12, 0, 0)

_CASES = json.loads(_FIXTURE.read_text())["cases"]


def _at(offset_ms: float) -> datetime:
    return _BASE + timedelta(milliseconds=offset_ms)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_busy_ms_matches_the_shared_corpus(case: dict) -> None:
    lo, hi = case["window"]
    spans = [(_at(s), _at(e)) for s, e in case["spans"]]
    assert busy_ms(spans, _at(lo), _at(hi)) == pytest.approx(case["expected_ms"])


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
