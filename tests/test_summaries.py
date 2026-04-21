"""Tests for evaluation.summaries.summarize_commands."""

from datetime import datetime

from coder_eval.evaluation.summaries import summarize_commands
from coder_eval.models import CommandTelemetry


def _make_cmd(tool_name="Bash", params=None, status="success", seq=0, result_summary=None):
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=f"tool_{seq}",
        timestamp=datetime.now(),
        parameters=params or {},
        result_status=status,
        sequence_number=seq,
        result_summary=result_summary,
    )


class TestSummarizeCommands:
    """Tests for the summarize_commands helper."""

    def test_empty_commands_returns_none(self):
        assert summarize_commands([]) is None

    def test_bash_command_shown(self):
        cmd = _make_cmd(tool_name="Bash", params={"command": "uip --help"})
        result = summarize_commands([cmd])
        assert "`uip --help`" in result
        assert "[success]" in result

    def test_read_file_path_shown(self):
        cmd = _make_cmd(tool_name="Read", params={"file_path": "/tmp/test.py"}, seq=0)
        result = summarize_commands([cmd])
        assert "/tmp/test.py" in result

    def test_grep_pattern_shown(self):
        cmd = _make_cmd(tool_name="Grep", params={"pattern": "def main"}, seq=0)
        result = summarize_commands([cmd])
        assert "pattern=def main" in result

    def test_result_preview_included(self):
        cmd = _make_cmd(params={"command": "ls"}, result_summary="file1.py\nfile2.py")
        result = summarize_commands([cmd])
        assert "→" in result
        assert "file1.py" in result

    def test_unknown_status_fallback(self):
        cmd = _make_cmd(status=None)
        result = summarize_commands([cmd])
        assert "[unknown]" in result

    def test_multiple_commands_numbered(self):
        cmds = [
            _make_cmd(tool_name="Bash", params={"command": "uip --help"}, seq=0),
            _make_cmd(tool_name="Bash", params={"command": "uip flow --help"}, seq=1),
            _make_cmd(tool_name="Read", params={"file_path": "out.json"}, seq=2),
        ]
        result = summarize_commands(cmds)
        lines = result.strip().split("\n")
        assert len(lines) == 3
        assert "1." in lines[0]
        assert "2." in lines[1]
        assert "3." in lines[2]

    def test_long_command_truncated(self):
        long_cmd = "x" * 200
        cmd = _make_cmd(params={"command": long_cmd})
        result = summarize_commands([cmd])
        # The backtick-delimited payload must be exactly 120 chars (not 200).
        assert "`" + "x" * 120 + "`" in result
        assert "`" + "x" * 121 not in result

    def test_sequence_gaps_produce_clean_numbering(self):
        """Enumerate-based numbering stays sequential even when sequence_numbers have gaps."""
        cmds = [
            _make_cmd(tool_name="Bash", params={"command": "echo a"}, seq=0),
            _make_cmd(tool_name="Bash", params={"command": "echo b"}, seq=5),
            _make_cmd(tool_name="Bash", params={"command": "echo c"}, seq=10),
        ]
        result = summarize_commands(cmds)
        lines = result.strip().split("\n")
        assert "1." in lines[0]
        assert "2." in lines[1]
        assert "3." in lines[2]
        # Ensure old gap-based numbers are NOT present
        assert "6." not in result
        assert "11." not in result

    def test_agent_tool_shown(self):
        """The Agent tool (renamed from Task) shows description."""
        cmd = _make_cmd(tool_name="Agent", params={"description": "search codebase"}, seq=0)
        result = summarize_commands([cmd])
        assert "(search codebase)" in result

    def test_task_tool_still_supported(self):
        """Legacy Task tool name (pre-2.1.75) is still handled."""
        cmd = _make_cmd(tool_name="Task", params={"description": "run tests"}, seq=0)
        result = summarize_commands([cmd])
        assert "(run tests)" in result
