"""Tests for command statistics calculation in analysis.py."""

from datetime import datetime

from coder_eval.analysis import calculate_command_statistics
from coder_eval.models import CommandTelemetry, TurnRecord


class TestAvgCommandTimeDivisor:
    """Verify avg_command_time_ms uses only commands with timing data as divisor."""

    def test_avg_time_excludes_commands_without_timing(self):
        """Average time should only consider commands with duration data."""
        now = datetime.now()
        commands = [
            CommandTelemetry(tool_name="Read", tool_id="1", timestamp=now, duration_ms=100.0),
            CommandTelemetry(tool_name="Write", tool_id="2", timestamp=now, duration_ms=200.0),
            CommandTelemetry(tool_name="Bash", tool_id="3", timestamp=now, duration_ms=None),
            CommandTelemetry(tool_name="Bash", tool_id="4", timestamp=now, duration_ms=None),
        ]

        turn = TurnRecord(iteration=1, user_input="test", agent_output="test", commands=commands)
        stats = calculate_command_statistics([turn])

        # Total time: 100 + 200 = 300, timed commands: 2, avg = 150
        assert stats.avg_command_time_ms == 150.0, (
            f"avg_command_time_ms = {stats.avg_command_time_ms}, expected 150.0. "
            f"Should divide by timed commands (2), not all commands (4)"
        )

    def test_zero_duration_counted_as_timed(self):
        """A command with duration_ms=0.0 should be counted as timed (not excluded)."""
        now = datetime.now()
        commands = [
            CommandTelemetry(tool_name="Read", tool_id="1", timestamp=now, duration_ms=0.0),
            CommandTelemetry(tool_name="Write", tool_id="2", timestamp=now, duration_ms=100.0),
            CommandTelemetry(tool_name="Bash", tool_id="3", timestamp=now, duration_ms=None),
        ]

        turn = TurnRecord(iteration=1, user_input="test", agent_output="test", commands=commands)
        stats = calculate_command_statistics([turn])

        # Total time: 0 + 100 = 100, timed commands: 2, avg = 50
        assert stats.avg_command_time_ms == 50.0, (
            f"avg_command_time_ms = {stats.avg_command_time_ms}, expected 50.0. duration_ms=0.0 should count as timed"
        )
