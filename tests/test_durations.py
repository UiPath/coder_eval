"""Unit tests for coder_eval.durations.format_ms.

`format_ms` moved out of `formatting.py` so the reports package does not inherit
a `claude_agent_sdk` dependency for a 14-line formatter. The `None`-vs-`0.0`
boundary is the CE058 contract — an unmeasured duration renders as a dash, a
measured zero as `0ms` — and is asserted explicitly here because throwing that
distinction away at the last step is exactly how one surface comes to print
`0ms` where another prints a dash.
"""

import pytest

from coder_eval.durations import format_ms


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (None, "—"),
        (0.0, "0ms"),
        (1.0, "1ms"),
        (999.4, "999ms"),
        (999.6, "1000ms"),
        (1000.0, "1.00s"),
        (1500.0, "1.50s"),
        (61_000.0, "61.00s"),
    ],
)
def test_format_ms(ms, expected):
    assert format_ms(ms) == expected


def test_unmeasured_and_measured_zero_are_distinguishable():
    """The whole point of the CE058 contract, in one assertion."""
    assert format_ms(None) != format_ms(0.0)
