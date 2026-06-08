"""Slack summary cli_version guard (coder_eval#382 follow-up).

`slack_summary.py` reads `cli_version` straight from run.json and is only ever
as clean as that file. The run-level aggregator drops junk, but a stale or
un-resummarised run.json could still hold a non-version `cli_version` (e.g. an
`{"Result": "Success"}` envelope from an older capture). `_clean_cli_version`
guards the channel ping so that junk never reaches the 300-person Slack channel.
"""

import importlib.util
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "dashboard" / "scripts" / "ci" / "slack_summary.py"
_spec = importlib.util.spec_from_file_location("slack_summary", _SCRIPT)
assert _spec and _spec.loader
slack_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slack_summary)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.196.0-alpha.20260605.7426", "1.196.0-alpha.20260605.7426"),
        # legitimate drift stays, each piece validated and kept
        (
            "1.196.0-alpha.20260605.7426 | 1.2.0-alpha.20260604.7394",
            "1.196.0-alpha.20260605.7426 | 1.2.0-alpha.20260604.7394",
        ),
        # the historical junk shape — envelope/array pieces dropped, version kept
        ('1.2.0-alpha.20260604.7394 | [] | {"Result": "Success"}', "1.2.0-alpha.20260604.7394"),
        # nothing version-shaped -> placeholder, never raw junk in the channel
        ('{"Result": "Success"}', "?"),
        ("", "?"),
        (None, "?"),
        (42, "?"),
    ],
)
def test_clean_cli_version(value, expected):
    assert slack_summary._clean_cli_version(value) == expected
