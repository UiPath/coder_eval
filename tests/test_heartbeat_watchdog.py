"""Unit tests for the container host-heartbeat staleness decision."""

import pytest

from coder_eval.cli.run_task_internal_command import heartbeat_is_alive


@pytest.mark.parametrize(
    ("current", "last_counter", "current_mtime", "last_mtime", "expected"),
    [
        # Counter advanced (mtime unchanged) — bind-mount mtime-lag arm.
        ("5", "4", 100.0, 100.0, True),
        # mtime advanced but counter still empty — startup-race arm.
        ("", "", 200.0, 100.0, True),
        # Both advanced.
        ("2", "1", 200.0, 100.0, True),
        # Neither advanced — stale candidate.
        ("3", "3", 100.0, 100.0, False),
        # Both empty/zero at startup — not alive (grace sleep governs startup).
        ("", "", 0.0, 0.0, False),
    ],
)
def test_heartbeat_is_alive(
    current: str, last_counter: str, current_mtime: float, last_mtime: float, expected: bool
) -> None:
    assert heartbeat_is_alive(current, last_counter, current_mtime, last_mtime) is expected
