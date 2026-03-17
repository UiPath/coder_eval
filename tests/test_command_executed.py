"""Tests for CommandExecutedCriterion."""

from datetime import datetime

from coder_eval.evaluation.checker import SuccessChecker
from coder_eval.models import CommandExecutedCriterion
from coder_eval.models.results import TurnRecord
from coder_eval.models.telemetry import CommandTelemetry


class MockSandbox:
    """Mock sandbox for testing (not used by CommandExecutedChecker but required by SuccessChecker)."""

    def __init__(self):
        self.sandbox_dir = None


def _make_command(
    tool_name: str = "Bash",
    parameters: dict | None = None,
    result_status: str = "success",
    tool_id: str = "tool-1",
) -> CommandTelemetry:
    """Helper to create a CommandTelemetry instance."""
    return CommandTelemetry(
        tool_name=tool_name,
        tool_id=tool_id,
        timestamp=datetime.now(),
        parameters=parameters or {},
        result_status=result_status,
    )


def _make_turn(commands: list[CommandTelemetry], iteration: int = 1) -> TurnRecord:
    """Helper to create a TurnRecord with commands."""
    return TurnRecord(
        iteration=iteration,
        user_input="test prompt",
        agent_output="test output",
        commands=commands,
    )


class TestCommandExecutedCriterion:
    """Test suite for CommandExecutedCriterion."""

    def test_match_found(self):
        """Test matching a Bash curl command with pattern."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://wttr.in/London"},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl to fetch weather",
            tool_name="Bash",
            command_pattern=r"curl.*wttr\.in",
            min_count=1,
            require_success=True,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert result.error is None
        assert "1/1" in result.details

    def test_no_match(self):
        """Test when commands exist but none match the pattern."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "ls -la"},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is None

    def test_no_turn_records(self):
        """Test when turn_records is None."""
        sandbox = MockSandbox()

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=None)

        assert result.score == 0.0
        assert result.error is not None
        assert "turn_records" in result.error

    def test_empty_commands(self):
        """Test when turns exist but have no commands."""
        sandbox = MockSandbox()
        turn_records = [_make_turn(commands=[])]

        criterion = CommandExecutedCriterion(
            description="Agent used any command",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert "No commands found" in result.details

    def test_tool_name_filter(self):
        """Test that tool_name filter only counts matching tools."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Read", parameters={"file_path": "main.py"}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "python main.py"}, tool_id="t2"),
                    _make_command(tool_name="Read", parameters={"file_path": "test.py"}, tool_id="t3"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used Read tool",
            tool_name="Read",
            min_count=2,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "2/2" in result.details

    def test_require_success(self):
        """Test that require_success filters out failed commands."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://api.example.com"},
                        result_status="error",
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl https://api.example.com"},
                        result_status="success",
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent successfully used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            require_success=True,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # Only 1 successful curl, need 2
        assert result.score == 0.5

    def test_partial_score(self):
        """Test fractional scoring when min_count > matches found."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "git add ."}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "git commit -m 'test'"}, tool_id="t2"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used git commands",
            tool_name="Bash",
            command_pattern=r"git",
            min_count=3,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # 2 matches out of 3 required
        assert abs(result.score - 2.0 / 3.0) < 0.01

    def test_invalid_regex(self):
        """Test that invalid regex pattern returns score=0.0 with error."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "ls"}),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Bad regex",
            command_pattern=r"[invalid",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is not None
        assert "Invalid regex" in result.error

    def test_no_filters(self):
        """Test that no tool_name/pattern matches all commands."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Read", parameters={"file_path": "a.py"}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": "ls"}, tool_id="t2"),
                    _make_command(tool_name="Write", parameters={"file_path": "b.py"}, tool_id="t3"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent executed any commands",
            min_count=3,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0

    def test_exclude_pattern_filters_help(self):
        """exclude_pattern should skip --help invocations."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip flow process get --help 2>&1"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip flow process get --process-key pk1 --feed-id fid1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+flow\s+process\s+get",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

    def test_exclude_pattern_all_excluded(self):
        """If all matches are excluded, score should be 0."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip flow process get --help"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip flow process get --help 2>&1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+flow\s+process\s+get",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_exclude_pattern_no_positive_matches(self):
        """exclude_pattern with no positive matches should still yield 0."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "ls -la"}, tool_id="t1"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            tool_name="Bash",
            command_pattern=r"curl",
            exclude_pattern=r"--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_exclude_pattern_invalid_regex(self):
        """Invalid exclude_pattern regex should return error."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": "uip flow process get pk1"}),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Bad exclude regex",
            command_pattern=r"uip",
            exclude_pattern=r"[invalid",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert result.error is not None
        assert "Invalid regex" in result.error

    def test_exclude_pattern_non_bash_tool(self):
        """Test exclude_pattern on non-Bash tool via JSON-serialized parameters."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "output.json", "content": '{"key": "value"}'},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "ignore.json", "content": '{"key": "value"}'},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent wrote a json file but not ignore.json",
            tool_name="Write",
            command_pattern=r"\.json",
            exclude_pattern=r"ignore\.json",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0

    def test_non_bash_tool(self):
        """Test matching non-Bash tool via JSON-serialized parameters."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Write",
                        parameters={"file_path": "output.json", "content": '{"key": "value"}'},
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent wrote output.json",
            tool_name="Write",
            command_pattern=r"output\.json",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
