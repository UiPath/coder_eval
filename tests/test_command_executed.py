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


def _make_turn(commands: list[CommandTelemetry], iteration: int = 1, crashed: bool = False) -> TurnRecord:
    """Helper to create a TurnRecord with commands."""
    return TurnRecord(
        iteration=iteration,
        user_input="test prompt",
        agent_output="test output",
        commands=commands,
        crashed=crashed,
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
        """Turns exist but have no commands ⇒ score by the same ``min_count`` math.

        Used to short-circuit on a separate ``"No commands found"`` branch, but
        that branch returned ``0.0`` even when ``min_count=0`` (the negative-
        assertion pattern), which was wrong. Now the empty case falls through
        to the normal scoring math: with ``min_count=1`` and zero matches, the
        score is ``0/1 = 0.0`` and the details mirror the positive shape.
        """
        sandbox = MockSandbox()
        turn_records = [_make_turn(commands=[])]

        criterion = CommandExecutedCriterion(
            description="Agent used any command",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert "0/1 required" in result.details

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

    def test_match_multiline_command(self):
        """`.` must span newlines so backslash-continued commands match."""
        sandbox = MockSandbox()
        multiline_cmd = (
            'uip is resources execute create "salesforce" "Contact" \\\n'
            '  --connection-id "abc-123" \\\n'
            '  --body \'{"LastName": "Smith"}\' \\\n'
            "  --output json"
        )
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": multiline_cmd},
                        result_status="success",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran execute create with --body",
            tool_name="Bash",
            command_pattern=r"uip\s+is\s+resources\s+execute\s+create.*--body",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert result.error is None

    def test_exclude_pattern_multiline(self):
        """`exclude_pattern` should also span newlines (DOTALL) for symmetry."""
        sandbox = MockSandbox()
        help_cmd = "uip foo \\\n  --help"
        real_cmd = "uip foo \\\n  --bar"
        turn_records = [
            _make_turn(
                [
                    _make_command(tool_name="Bash", parameters={"command": help_cmd}, tool_id="t1"),
                    _make_command(tool_name="Bash", parameters={"command": real_cmd}, tool_id="t2"),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip foo (not --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+foo",
            exclude_pattern=r"foo.*--help",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

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
                        parameters={"command": "uip maestro flow process get --help 2>&1"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --process-key pk1 --feed-id fid1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip maestro flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+maestro\s+flow\s+process\s+get",
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
                        parameters={"command": "uip maestro flow process get --help"},
                        tool_id="t1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip maestro flow process get --help 2>&1"},
                        tool_id="t2",
                    ),
                ]
            )
        ]

        criterion = CommandExecutedCriterion(
            description="Agent ran uip maestro flow process get (not just --help)",
            tool_name="Bash",
            command_pattern=r"uip\s+maestro\s+flow\s+process\s+get",
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
                    _make_command(tool_name="Bash", parameters={"command": "uip maestro flow process get pk1"}),
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

    def test_crashed_turn_commands_are_counted(self):
        """Commands from crashed partial turns count toward min_count.

        Scenario: a crash preserves 3 commands; the retry adds 2 more. The
        retry continues from where the crash left off (session-resume + same
        sandbox), so all 5 calls are real executed work and all 5 should
        satisfy a min_count=4 requirement.
        """
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://a"},
                        tool_id="t-crash-1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://b"},
                        tool_id="t-crash-2",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://c"},
                        tool_id="t-crash-3",
                    ),
                ],
                crashed=True,
            ),
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://d"},
                        tool_id="t-clean-1",
                    ),
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://e"},
                        tool_id="t-clean-2",
                    ),
                ],
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl at least 4 times",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # All 5 calls (3 from partial + 2 from retry) count → 5/4 capped to 1.0.
        assert result.score == 1.0
        assert "5/4" in result.details

    def test_all_turns_crashed_commands_still_counted(self):
        """Commands from an all-crashed run are real work and must be counted."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "curl http://a"},
                        tool_id="t-crash-1",
                    ),
                ],
                crashed=True,
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent used curl",
            command_pattern=r"curl",
            min_count=1,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "1/1" in result.details

    # ------------------------------------------------------------------
    # max_count + min_count=0 negative-assertion patterns.
    # Skills task YAMLs (uipath-skills) use these to express "must NOT call
    # the retired command". Before max_count landed, those YAMLs failed
    # pydantic validation in the `Validate Skills Task YAMLs` CI gate.
    # ------------------------------------------------------------------

    def test_negative_assertion_passes_when_no_match(self):
        """min_count=0, max_count=0 ⇒ pass iff the pattern never matched."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip admin users list --search alice"},
                        tool_id="t-ok-1",
                    ),
                ]
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent did NOT use the retired `uip or users list` path",
            tool_name="Bash",
            command_pattern=r"uip\s+or\s+users\s+list",
            min_count=0,
            max_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "allowed range 0..0" in result.details

    def test_negative_assertion_fails_when_pattern_matched(self):
        """min_count=0, max_count=0 ⇒ a single retired-call match fails the gate."""
        sandbox = MockSandbox()
        turn_records = [
            _make_turn(
                [
                    _make_command(
                        tool_name="Bash",
                        parameters={"command": "uip or users list"},
                        tool_id="t-retired-1",
                    ),
                ]
            ),
        ]

        criterion = CommandExecutedCriterion(
            description="Agent did NOT use the retired `uip or users list` path",
            tool_name="Bash",
            command_pattern=r"uip\s+or\s+users\s+list",
            min_count=0,
            max_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0
        assert "allowed range 0..0" in result.details

    def test_bounded_range_pass_inside(self):
        """min_count=2, max_count=4 ⇒ 3 matches sits inside the range."""
        sandbox = MockSandbox()
        commands = [
            _make_command(
                tool_name="Bash",
                parameters={"command": "curl https://api/x"},
                tool_id=f"t-{i}",
            )
            for i in range(3)
        ]
        turn_records = [_make_turn(commands)]

        criterion = CommandExecutedCriterion(
            description="Agent retried within bounds",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            max_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 1.0
        assert "allowed range 2..4" in result.details

    def test_bounded_range_fails_when_over_cap(self):
        """min_count=2, max_count=4 ⇒ 5 matches busts the cap (score 0.0)."""
        sandbox = MockSandbox()
        commands = [
            _make_command(
                tool_name="Bash",
                parameters={"command": "curl https://api/x"},
                tool_id=f"t-{i}",
            )
            for i in range(5)
        ]
        turn_records = [_make_turn(commands)]

        criterion = CommandExecutedCriterion(
            description="Agent must not retry more than 4 times",
            tool_name="Bash",
            command_pattern=r"curl",
            min_count=2,
            max_count=4,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        assert result.score == 0.0

    def test_min_count_zero_no_max_is_trivially_satisfied(self):
        """min_count=0 with no max_count ⇒ score 1.0 even when no commands match."""
        sandbox = MockSandbox()
        turn_records = [_make_turn([])]

        criterion = CommandExecutedCriterion(
            description="Optional command (passes vacuously)",
            tool_name="Bash",
            command_pattern=r"never-matches",
            min_count=0,
        )

        checker = SuccessChecker(sandbox)
        result = checker.check(criterion, turn_records=turn_records)

        # No turn-records-empty short-circuit: turns exist but commands is [].
        # Score should be 1.0 by the min_count==0 rule, not 0/0 ZeroDivisionError.
        assert result.score == 1.0

    def test_invalid_range_rejected_at_model_level(self):
        """max_count < min_count must be rejected by the Pydantic validator."""
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError, match=r"max_count.*must be >= min_count"):
            CommandExecutedCriterion(
                description="impossible range",
                command_pattern="x",
                min_count=5,
                max_count=2,
            )
